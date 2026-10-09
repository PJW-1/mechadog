"""전역 탐색 워커 — 지도 전체 정합을 루프 스레드 밖에서 돌린다 (순찰 측위 보조).

`global_match` 는 지도 전체를 훑어 실기 PC 에서도 **1~2초**가 걸린다. `observe_scan` 이
명령 루프 스레드에서 도는데 여기서 동기로 기다리면 명령 간격이 온보드 워치독(600ms)을
넘어 `ONBOARD_FAILSAFE` 를 낸다(2026-10-04 실측: 틱 1.9s → 명령 거부 → 래치). 그래서
전역 탐색만 데몬 스레드로 보내고 결과는 루프 스레드가 `take` 로 가져와 적용한다 —
**상태 변경은 루프 스레드에서만** 일어나는 규칙을 지키면서 명령은 계속 나간다.

이 클래스는 사서함만 쥔다. 무엇을 탐색할지(지도 사본·거름·복원 기준)와 결과 해석
(투표·채택·지도 쓰기)은 호출자 몫이다. 워커는 요청에 실린 지도 사본만 읽으므로
적분·팽창·복원이 워커가 읽는 셀/메타를 바꿀 수 없다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np

from host.common.lidar_link import Scan
from host.common.logging_setup import event_logger
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchResult, Pose

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")

#: 범위 거름 — 후보 셀 좌표(x, y 배열)를 받아 허용 여부 배열을 돌려준다.
Allowed = Callable[[np.ndarray, np.ndarray], np.ndarray]
#: 요청 — 종류, 점, 스캔, 요청 때 자세, 독립된 읽기 전용 지도 사본.
Request = tuple[str, np.ndarray, Scan, Pose, OccupancyGrid]
#: 결과 — (종류, MatchResult|None, 요청 점, 요청 스캔, 요청 때 자세).
Result = tuple[str, MatchResult | None, np.ndarray, Scan, Pose]
#: 요청 맥락 — (신선한 IMU yaw | None, 이동 명령 수, 측위 세대, monotonic ms).
Context = tuple[float | None, int, int, int]


class GlobalMatchWorker:
    """전역 탐색 한 건짜리 사서함과 그것을 비우는 데몬 스레드.

    `inflight`·`context` 는 루프 스레드만 만진다. `request*`·`result`·`prior_result` 는
    `cv` 로 보호한다.
    """

    def __init__(
        self,
        search: Callable[[OccupancyGrid, np.ndarray, Allowed | None], MatchResult | None],
        restore: Callable[[OccupancyGrid, np.ndarray, Pose], MatchResult],
    ) -> None:
        #: 지도 전역 탐색 — 워커 스레드에서 부른다.
        self._search = search
        #: 복원 기준 둘레의 창 안 정합 — 전역 결과가 있을 때만 워커 스레드에서 부른다.
        self._restore = restore
        #: 제출됐지만 아직 루프가 소비하지 않은 전역 탐색이 있는가 (루프 스레드만 만진다).
        self.inflight: bool = False
        #: 요청 사서함.
        self.request: Request | None = None
        #: 워커에 넘긴 복원 기준 자세 (`cv` 로 보호).
        self.request_prior: Pose | None = None
        #: 워커에 넘긴 탐색 범위 거름 (사람이 알려준 구역) — `cv` 로 보호.
        self.request_allowed: Allowed | None = None
        #: 워커가 둔 결과.
        self.result: Result | None = None
        #: 워커가 낸 창 안 정합 결과 (`cv` 로 보호).
        self.prior_result: MatchResult | None = None
        #: 요청 때의 맥락 — 결과가 낡았는지 루프 스레드가 본다.
        self.context: Context | None = None
        self.cv = threading.Condition(threading.Lock())
        self.thread: threading.Thread | None = None

    def ensure_started(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(
            target=self._main,
            name="patrol-global-match",
            daemon=True,
        )
        self.thread.start()

    def _main(self) -> None:
        while True:
            with self.cv:
                while self.request is None:
                    self.cv.wait()
                kind, points, scan, asked_pose, match_grid = self.request
                prior = self.request_prior
                allowed = self.request_allowed
                self.request = None
                self.request_prior = None
                self.request_allowed = None
            try:
                result: MatchResult | None = self._search(match_grid, points, allowed)
            except Exception as exc:  # noqa: BLE001 — 결과를 안 두면 inflight 가 영영 안 풀린다
                LOG.error("global_match_failed", error=f"{type(exc).__name__}: {exc}")
                result = None
            prior_result = None
            if prior is not None and result is not None:
                try:
                    prior_result = self._restore(match_grid, points, prior)
                except Exception as exc:  # noqa: BLE001 — 복원은 덤이다, 전역 결과는 살린다
                    LOG.error("restore_match_failed", error=f"{type(exc).__name__}: {exc}")
            with self.cv:
                self.prior_result = prior_result
                self.result = (kind, result, points, scan, asked_pose)

    def submit(
        self,
        request: Request,
        context: Context,
        prior: Pose | None,
        allowed: Allowed | None,
    ) -> None:
        """요청을 사서함에 넣고 워커를 깨운다. 호출자가 먼저 `inflight` 를 확인한다."""
        self.ensure_started()
        self.inflight = True
        self.context = context
        with self.cv:
            self.request = request
            self.request_prior = prior
            self.request_allowed = allowed
            self.cv.notify()

    def take(self) -> tuple[Result | None, MatchResult | None]:
        """워커가 둔 결과와 창 안 정합 결과를 꺼낸다. 결과가 있으면 `inflight` 를 푼다."""
        with self.cv:
            done = self.result
            prior_result = self.prior_result
            self.result = None
            self.prior_result = None
        if done is not None:
            self.inflight = False
        return done, prior_result
