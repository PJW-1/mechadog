"""측위 신뢰 관리 — 내장 스캔 정합의 추적·전역 재측위·신뢰 만료와 복원 (순찰 측위).

`PatrolController` 가 «어디에 있는가» 를 묻는 상태를 여기에 모은다.

- **추적** — 스캔마다 국소 창 정합으로 자세를 잇는다 (`track_scan`).
- **전역 탐색** — 상실 중 재측위와 정지 중 감사를 `GlobalMatchWorker` 로 보내고, 결과를
  루프 스레드에서 투표·채택한다 (`poll_global`).
- **신뢰** — 전역 확인(`verified`)·만료(`expire_trust`)·제자리 복원(`try_restore`).
- **사람의 힌트** — 대시보드 «위치 알려주기» 의 구역·점 (`hint_zone`·`hint_point`).

설정값(`reloc_votes`·`verify_interval_ms` 등)·자세(`pose`)·시드 표시(`pose_seeded`)는
순찰기의 것을 그때그때 읽는다 — 시험과 런타임이 생성 뒤에 바꾸기 때문이다. 지도 적분·
장애물 투영·마스크 재생성은 순찰기에 남아 있어 그쪽을 부른다. 상태 변경은 순찰 루프
스레드에서만 일어난다.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from host.behavior.reloc_worker import GlobalMatchWorker
from host.common.lidar_link import Scan
from host.common.logging_setup import event_logger
from host.common.units import deg_to_rad, rad_to_deg, wrap_pi
from host.slam.occupancy import OccupancyGrid
from host.slam.scan_match import MatchParams, MatchResult, Pose, global_match, match, preprocess

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController

#: 순찰 컨트롤러와 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.behavior.patrol")


class LocalizationTrust:
    """순찰기의 측위 상태와 그 신뢰를 쥔다."""

    def __init__(self, patrol: PatrolController) -> None:
        self._patrol = patrol
        #: 전역 탐색 데몬 스레드와 사서함 (`host/behavior/reloc_worker.py`).
        self.worker = GlobalMatchWorker(self.global_search, self.restore_search)
        #: 마지막으로 `pose` 를 갱신한 시각 — 정합에 실패한 스캔은 바꾸지 않는다.
        self.last_pose_ms: int | None = None
        #: 다음 지도 전역 재측위를 허용하는 시각 — 비싼 탐색이라 주기를 제한한다.
        self.reloc_next_ms: int = 0
        #: 다음 정지 중 전역 감사 시각.
        self.verify_next_ms: int = 0
        #: 마지막 정합 시도의 적중 비율 — 진단·불신 판정에 쓴다.
        self.match_frac: float = 0.0
        #: 현재 자세가 지도 전역 탐색으로 확인됐는가 — 참일 때만 스캔을 지도에 적분한다.
        #: 국소 추적만으로는 틀린 자리를 자신 있게 따라갈 수 있어, 그 동안의 적분은 지도를
        #: 틀린 모양으로 굳힌다. 전역 확인 전에는 지도를 읽기만 한다.
        self.verified: bool = False
        #: 마지막 전역 확인 시점의 지도 — 그 뒤 적분이 틀린 것으로 드러나면 여기로 되돌린다.
        self.verified_snapshot: tuple[np.ndarray, Any] | None = None
        #: 마지막으로 알린 구역 — 바뀔 때만 `zone_entered` 를 남긴다.
        self.last_zone: str | None = None
        #: 연속된 전역 탐색 결과 — 서로 0.3m 안에서 `reloc_votes` 번 모이면 채택한다.
        self.global_votes: list[Pose] = []
        #: 측위 세대 — 신뢰 만료 때 올린다. 이전 세대에 요청한 전역 결과는 버린다 (리뷰 지적).
        self.loc_epoch: int = 0
        #: 마지막 표 뒤에 로봇이 움직였는가 (MOVE 송신 또는 IMU 방위 변화) — 표의 독립성.
        self.moved_since_vote: bool = True
        self.imu_at_vote: float | None = None
        #: 실제로 나간 이동 명령(MOVE 0 이 아님)의 누적 수 — 비동기 결과가 낡았는지 본다.
        self.move_seq: int = 0
        #: 신뢰를 잃었다고 기록했는가 (`trust_expiry_ms` 넘은 상실).
        self.trust_expired: bool = False
        #: 마지막 `observe_pose` 때의 신선한 IMU yaw (없으면 None) — 복원 기준의 방위.
        self.last_pose_imu: float | None = None
        #: 마지막 `observe_pose` 때의 이동 명령 수·몸체 기울기(pitch, roll rad) — 복원 기준의 시점.
        self.last_pose_moves: int = 0
        self.last_pose_tilt: tuple[float, float] | None = None
        #: 가장 최근 텔레메트리의 (pitch, roll) rad.
        self.tilt: tuple[float, float] | None = None
        #: 신뢰 복원 기준 — (확인된 마지막 자세, 그때 IMU yaw, 그 시각, 그때 이동 명령 수, 기울기).
        self.restore_anchor: tuple[Pose, float, int, int, tuple[float, float] | None] | None = None
        self.restore_votes: list[Pose] = []
        #: 사람이 알려준 구역과 그 시각 — 그 구역 안에서만 전역 재측위한다.
        self.zone_hint: tuple[str, int] | None = None
        #: 사람이 찍은 현재 위치 (순찰 좌표 x, y, Host 시각).
        self.point_hint: tuple[float, float, int] | None = None
        #: 마지막으로 «검증됨» 상태에서 자세를 갱신한 시각.
        self.last_verified_pose_ms: int | None = None
        #: 지금 적용 중인 전역 결과의 요청 시점 IMU (`poll_global` 이 채운다).
        self.result_imu: float | None = None
        #: 자세가 **내장 스캔 정합**(`track_scan`)에서 나오는가. 외부 측위(ROS2 `MAP_POSE`·
        #: 시뮬레이션)가 `observe_pose` 로 넣는 자세는 그쪽이 보증하므로 확인 보류를
        #: 적용하지 않는다.
        self.own_localization: bool = False

    @property
    def untrusted(self) -> bool:
        """내장 정합이 아직 자세를 믿지 못하는가 — 전역 확인도 사람 시드도 없다."""
        return self.own_localization and not (self.verified or self._patrol.pose_seeded)

    def new_epoch(self) -> None:
        """이전 요청의 전역 결과와 표를 무효로 한다 (자세 명령·흔들린 스캔)."""
        self.loc_epoch += 1
        self.global_votes.clear()
        self.restore_votes.clear()

    def note_move(self) -> None:
        """실제 이동 명령이 나갔다 — 표의 독립성과 비동기 결과의 낡음을 가른다."""
        self.moved_since_vote = True
        self.move_seq += 1

    def observe_tilt(self, pitch_deg: float, roll_deg: float) -> None:
        """몸체 기울기를 받는다. 복원 기준보다 크게 기울면 «들어 올렸다» 로 보고 기준을 버린다."""
        self.tilt = (deg_to_rad(pitch_deg), deg_to_rad(roll_deg))
        anchor = self.restore_anchor
        limit = self._patrol.reloc_restore_tilt_rad
        if (
            anchor is not None
            and anchor[4] is not None
            and (
                abs(self.tilt[0] - anchor[4][0]) > limit or abs(self.tilt[1] - anchor[4][1]) > limit
            )
        ):
            self.drop_restore_anchor("lifted")

    def observe_pose(self, pose: Pose, now_ms: int) -> None:
        """새 자세를 반영한다 — 외부 측위(WBS 5.4.4)와 내장 정합이 같은 길로 들어온다."""
        if not all(math.isfinite(value) for value in pose):
            return
        self._patrol.pose = (float(pose[0]), float(pose[1]), wrap_pi(float(pose[2])))
        self.last_pose_ms = now_ms
        if self._patrol.pose_verified:
            self.last_verified_pose_ms = now_ms
        self.last_pose_imu = self._patrol.heading.anchor_pose(now_ms)
        self.last_pose_moves = self.move_seq
        self.last_pose_tilt = self.tilt
        self.trust_expired = False
        zone = self._patrol.current_zone
        if zone != self.last_zone:
            self.last_zone = zone
            LOG.info(
                "zone_entered",
                zone=zone,
                x=round(self._patrol.pose[0], 2),
                y=round(self._patrol.pose[1], 2),
            )
        self._patrol.heading.align_offset()
        # 재개는 step의 전체 관문에서 한다. 새 tf 하나가 스캔 두절이나
        # 미관측 셀 정지를 해제해서는 안 된다.

    def track_scan(self, scan: Scan, now_ms: int) -> None:
        """받아들인 스캔으로 측위한다 — 전역 결과 적용·재측위·감사·국소 추적 순서."""
        self.own_localization = True
        self.expire_trust(now_ms)
        if not self._patrol._local_scan.clear_allowed:
            self.new_epoch()
            return
        _t_all = time.perf_counter()
        points = preprocess(scan.points, *self._patrol.range_m)
        if not points.size:
            return
        self._patrol._scan_now_ms = now_ms
        # 워커가 끝낸 전역 탐색 결과를 먼저 가져온다 — 비어 있으면 즉시 돌아간다.
        _t0 = time.perf_counter()
        self.poll_global(now_ms)
        _t_poll = time.perf_counter() - _t0
        # ⚠️ **전역 탐색이 도는 동안에도 국소 추적을 멈추지 않는다.** 예전엔 파이썬 루프
        # 탐색이 GIL 을 쥐어 국소 정합이 17ms→1~2.5s 로 부풀었고, 그래서 탐색 동안 국소
        # 정합을 쉬게 했다 — 그것이 정지 감사마다(15초) 약 3.3초 LOST 를 만들었다(live_4
        # 26회). 탐색을 벡터화해 같은 시간 국소 정합이 9ms 로 유지되므로 쉴 이유가 없다.
        if self._patrol.pose_stale(now_ms) and self._patrol.reloc_interval_ms > 0:
            # 한 번도 못 잡았거나 추적이 끊긴 상태 — 국소 창은 추정 위치가 틀리면
            # 답을 못 찾으니 지도 전체를 거칠게 훑어 다시 잠근다 (FR-6.6).
            # 비싼 탐색이라 주기를 제한한다.
            _t1 = time.perf_counter()
            submitted = False
            if now_ms >= self.reloc_next_ms:
                self.reloc_next_ms = now_ms + self._patrol.reloc_interval_ms
                self.relocalize(points, scan, now_ms)
                submitted = True
            _t_stale = time.perf_counter() - _t1
            if _t_stale > 0.2:
                LOG.warning(
                    "observe_scan_stale_slow",
                    ms=round(_t_stale * 1000, 1),
                    seeded=self._patrol.pose_seeded,
                    last_pose=self.last_pose_ms is not None,
                )
            # 탐색을 던졌거나 잡힌 자세 자체가 없으면 여기서 끝. 자세가 있으면 계속
            # 내려가 국소 창도 돌린다 — 재측위(~1.5초)가 `pose_timeout_ms`(0.5초)보다
            # 길어 상실 표시가 붙는 사이에도, 시드·갱신된 자세라면 여기서 신선도가
            # 회복되어 상실이 풀린다 (자세가 정말 틀렸으면 점수 0 이라 갱신이 없어
            # 상실이 유지되고 다음 전역 탐색이 다시 찾는다). 이 fallthrough 가 없으면
            # 전역 탐색이 국소 정합을 영원히 굶기는 기아 루프가 된다 (2026-10-03 실측).
            if self.last_pose_ms is None:
                return
            del submitted
        # ── 정지 중 주기 감사 ── 국소 창만으론 «틀린 자리에 수렴한 잠금»을 스스로
        # 못 푼다. 서 있는 동안 지도 전체를 훑어 현재 자세가 전역으로도 맞는지 본다.
        if (
            self._patrol.verify_interval_ms > 0
            and now_ms >= self.verify_next_ms
            and self._patrol._is_stationary()
        ):
            self.verify_next_ms = now_ms + self._patrol.verify_interval_ms
            self.verify_pose(points, scan, now_ms)
        # 아직 한 번도 정합되지 않았으면 넓은 창으로 찾는다 — 시드 자세가 배치 오차만큼
        # 어긋나 있어도 첫 고정은 잡히게. 잡힌 뒤에는 좁은 창으로 추적한다.
        # 변화량만 넘긴다 — 절대 yaw 는 지도 좌표계와 옵셋이 있다.
        yaw_delta = self._patrol.heading.consume_yaw_delta(now_ms)
        params = self._patrol.match_params
        if self.last_pose_ms is None and self._patrol.first_match_params is not None:
            params = self._patrol.first_match_params
        elif self._patrol.heading.delta_fresh and self._patrol.imu_match_params is not None:
            # 신선한 IMU 가 회전량을 쟀다 — 방위는 IMU 예측 근처에서만 찾는다.
            params = self._patrol.imu_match_params
        _t2 = time.perf_counter()
        result = match(
            self._patrol.match_grid, points, self._patrol.pose, params, yaw_delta=yaw_delta
        )
        _t_match = time.perf_counter() - _t2
        if _t_match > 0.2:
            LOG.warning(
                "observe_scan_match_slow",
                ms=round(_t_match * 1000, 1),
                first=params is self._patrol.first_match_params,
                window_m=params.search_lin_m,
            )
        self.match_frac = result.score / len(points)
        if result.skipped or result.score == 0:
            # 정합 실패 = 측위 상실 (FR-6.6). 점수 0 인 후보로 자세를 갱신하지 않는다.
            return
        # ⚠️ 약한 정합(frac 낮음)으로 추적을 끊지 않는다 — 가구 다리가 덜 그려진 자리에서는
        # **참 위치도 점수가 낮다.** 끊으면 재측위가 더 높은 점수의 엉뚱한 벽으로 로봇을
        # 옮긴다(실측 재현). 자세의 신뢰는 frac 이 아니라 전역 감사의 반복 일치
        # (`pose_verified`)로 판단하고, 지도 기록·자율 주행이 그 플래그를 본다.
        self._patrol.observe_map_pose(result.pose, now_ms)
        self._patrol._grow_map(points, result.score)
        _t0 = time.perf_counter()
        self._patrol._check_new_obstacle(scan)
        _t_obs = time.perf_counter() - _t0
        if _t_obs > 0.2 or _t_poll > 0.2:
            LOG.warning(
                "observe_scan_phase_slow",
                poll_ms=round(_t_poll * 1000, 1),
                obstacle_ms=round(_t_obs * 1000, 1),
            )

    def expire_trust(self, now_ms: int) -> None:
        """측위가 `trust_expiry_ms` 넘게 끊겼다 — «검증됨»·사람 시드의 신뢰를 버린다.

        그 사이 로봇이 들려 옮겨졌을 수 있어, 예전 확인이나 처음 받은 시드를 근거로 계속
        주행·지도 기록을 허용하면 안 된다. 자세값 자체는 두고(전역 탐색의 출발점) 신뢰만 내린다.
        """
        if (
            self._patrol.trust_expiry_ms <= 0
            or self.trust_expired
            or self.last_pose_ms is None
            or now_ms - self.last_pose_ms <= self._patrol.trust_expiry_ms
        ):
            return
        self.trust_expired = True
        self.loc_epoch += 1
        if (
            self._patrol.reloc_restore_enabled
            and self.verified
            and self.last_pose_imu is not None
            and self.last_pose_tilt is not None
        ):
            # 사람 시드는 기준으로 쓰지 않는다 — 전역 확인된 자세만 «제자리» 의 근거다.
            # 이동 수·기울기는 **자세를 관측한 시점** 것 — 그 뒤 나간 MOVE 는 «이동» 이다.
            self.restore_anchor = (
                self._patrol.pose,
                self.last_pose_imu,
                self.last_pose_ms,
                self.last_pose_moves,
                self.last_pose_tilt,
            )
            self.restore_votes.clear()
            LOG.info(
                "restore_anchor_set",
                x=round(self._patrol.pose[0], 2),
                y=round(self._patrol.pose[1], 2),
                yaw_deg=round(rad_to_deg(self._patrol.pose[2]), 1),
            )
        if self.verified or self._patrol.pose_seeded:
            LOG.warning(
                "localization_trust_expired",
                lost_ms=now_ms - self.last_pose_ms,
                was_verified=self.verified,
                was_seeded=self._patrol.pose_seeded,
            )
        self.verified = False
        self._patrol.pose_seeded = False
        self.global_votes.clear()

    def min_match_score(self, points: np.ndarray) -> int:
        """**새 고정**(전역 재측위·시드 시작)의 채택 하한 — 연속 추적에는 적용하지 않는다."""
        return max(1, math.ceil(self._patrol.min_match_frac * len(points)))

    def relocalize(self, points: np.ndarray, scan: Scan, now_ms: int) -> None:
        """지도 전역 재측위 — 국소 창이 풀 수 없는 잠김(오정합·임의 배치)을 연다.

        신뢰 순서는 **사람이 준 시드 > 전역 탐색 > 국소 추적**이다. 지도가 아직 덜 채워진
        자리(가구 다리·책상 밑)에서는 전역 최적이 엉뚱한 벽에 얹힐 수 있어, 첫 고정은
        시드가 있으면 시드 주변에서 잡고 전역 탐색은 그 뒤 **감사**로만 쓴다.
        """
        if self.last_pose_ms is None and self.seed_fallback(points, scan, now_ms):
            return
        self.submit_global("reloc", points, scan)

    # ── 전역 탐색 비동기 실행 ─────────────────────────────────
    # 동기로 돌리면 1~2초 동안 명령이 멈춰 온보드 워치독이 `ONBOARD_FAILSAFE` 를
    # 건다 — 탐색만 워커 스레드(`GlobalMatchWorker`)로 보내고, 결과 해석(투표·채택·
    # 지도 쓰기·장애물 표시)은 루프 스레드가 `poll_global` 에서 한다. 워커는
    # `match_grid` 를 요청 시점의 독립 사본만 읽는다. 적분·팽창·복원이 워커의 셀/메타를
    # 바꿀 수 없다.

    def global_search(
        self,
        match_grid: OccupancyGrid,
        points: np.ndarray,
        allowed: Callable[[np.ndarray, np.ndarray], np.ndarray] | None,
    ) -> MatchResult | None:
        """워커 스레드에서 도는 지도 전역 탐색 — 설정값은 탐색하는 그때 읽는다."""
        return global_match(
            match_grid,
            points,
            lin_step_m=self._patrol.global_match_lin_step_m,
            ang_step_rad=self._patrol.global_match_ang_step_rad,
            occ_thresh=self._patrol.match_params.occ_thresh,
            min_known_cells=self._patrol.match_params.min_known_cells,
            free_thresh=self._patrol.plan_params.free_thresh,
            sigma_m=self._patrol.match_params.sigma_m,
            full_scan_ambiguity=self._patrol.global_full_scan_ambiguity,
            allowed=allowed,
        )

    def restore_search(
        self, match_grid: OccupancyGrid, points: np.ndarray, prior: Pose
    ) -> MatchResult:
        """워커 스레드에서 도는 복원 기준 둘레 창 안 정합."""
        return match(match_grid, points, prior, self.restore_params())

    def submit_global(self, kind: str, points: np.ndarray, scan: Scan) -> bool:
        """전역 탐색을 워커에 맡긴다. 이미 한 건이 진행 중이면 놓친다(False)."""
        if self.worker.inflight:
            return False
        cells, meta = self._patrol.match_grid.snapshot()
        cells.setflags(write=False)
        match_grid = OccupancyGrid(meta, cells)
        context = (
            self._patrol.safety.yaw_rad
            if self._patrol.heading.imu_is_fresh(self._patrol._scan_now_ms)
            else None,
            self.move_seq,
            self.loc_epoch,
            self._patrol.wall_clock_ms(),
        )
        prior = self.restore_prior(self._patrol._scan_now_ms) if kind == "reloc" else None
        allowed = self.zone_filter(self._patrol._scan_now_ms) if kind == "reloc" else None
        self.worker.submit(
            (kind, points.copy(), scan, self._patrol.pose, match_grid), context, prior, allowed
        )
        return True

    def hint_zone(self, zone: str, now_ms: int) -> bool:
        """사람이 «로봇은 지금 이 구역 안» 이라고 알려줬다. **루프 스레드에서만** 부른다.

        지금 믿던 자세는 버린다 — 사람이 다른 곳이라고 했으니 국소 추적·복원 기준·표가 모두
        근거를 잃는다. 다음 전역 재측위는 그 구역 안 후보만 보고(경쟁 후보도 그 안에서만 센다),
        평소처럼 `reloc_votes` 표가 모여야 채택된다. 구역 안에서도 모호하면 계속 선다.
        """
        if zone not in self._patrol.zones.labels:
            LOG.warning("zone_hint_unknown", zone=zone)
            return False
        self.zone_hint = (zone, now_ms)
        self.point_hint = None
        x, y = self._patrol.zones.xy(zone)
        self.reset_location_hint(x, y, "zone_hint")
        LOG.warning("zone_hint", zone=zone, hint="이 구역 안에서만 위치를 다시 찾는다")
        return True

    def validate_hint_point(self, x: float, y: float) -> tuple[bool, str]:
        """사람이 알려준 현재 위치가 지도 안의 비점유 셀인가. 자세·신뢰는 바꾸지 않는다."""
        if not math.isfinite(x) or not math.isfinite(y):
            return False, "위치 좌표는 유한한 숫자여야 한다"
        row, col = self._patrol.grid.to_cell(x, y)
        if not self._patrol.grid.inside(row, col):
            return False, "알려준 위치가 지도 밖이다"
        if self._patrol.grid.cells[row, col] >= self._patrol.plan_params.occ_thresh:
            return False, "알려준 위치가 장애물 칸이다 — 로봇이 있는 빈 곳을 찍어 주세요"
        return True, ""

    def hint_point(self, x: float, y: float, now_ms: int) -> bool:
        """지도에서 알려준 점 주변만 전역 탐색한다. **루프 스레드에서만** 부른다.

        점은 표시용 자세일 뿐이다. 기존 구역 힌트처럼 신뢰·복원 기준을 버리고 멈추며,
        같은 표결·모호성 관문을 통과해야 실제 자세로 채택한다.
        """
        accepted, detail = self.validate_hint_point(x, y)
        if not accepted:
            LOG.warning("point_hint_rejected", reason=detail)
            return False
        self.point_hint = (x, y, now_ms)
        self.zone_hint = None
        self.reset_location_hint(x, y, "point_hint")
        LOG.warning("point_hint", x=x, y=y, radius_m=self._patrol.point_hint_radius_m)
        return True

    def reset_location_hint(self, x: float, y: float, reason: str) -> None:
        """사람의 새 위치 힌트로 이전 측위 근거를 버린다. xy만 표시하고 방위는 유지한다."""
        self.loc_epoch += 1  # 이미 날아간 전역 탐색 결과는 낡았다
        self.last_pose_ms = None
        self.verified = False
        self._patrol.pose_seeded = False
        self.global_votes.clear()
        self.drop_restore_anchor(reason)
        self.reloc_next_ms = 0
        self._patrol.pose = (x, y, self._patrol.pose[2])
        self._patrol.commander.halt()

    def zone_filter(self, now_ms: int) -> Callable[[np.ndarray, np.ndarray], np.ndarray] | None:
        point = self.point_hint
        if point is not None:
            x, y, since_ms = point
            if now_ms - since_ms > self._patrol.zone_hint_ms:
                self.point_hint = None
                LOG.warning("point_hint_expired", hint="찍은 점 주변에서도 위치를 못 찾았다")
                return None
            radius = self._patrol.point_hint_radius_m
            return lambda xs, ys: np.hypot(xs - x, ys - y) <= radius
        hint = self.zone_hint
        if hint is None:
            return None
        zone, since_ms = hint
        if now_ms - since_ms > self._patrol.zone_hint_ms:
            self.zone_hint = None
            LOG.warning("zone_hint_expired", zone=zone, hint="구역 안에서도 위치를 못 찾았다")
            return None
        zone_map = self._patrol.zone_map
        if zone_map is not None and zone in zone_map.names.values():
            return lambda xs, ys: zone_map.contains(zone, xs, ys)
        ax, ay = self._patrol.zones.xy(zone)
        radius = self._patrol.zone_hint_radius_m
        return lambda xs, ys: np.hypot(xs - ax, ys - ay) <= radius

    def restore_params(self) -> MatchParams:
        return MatchParams(
            search_lin_m=self._patrol.reloc_restore_radius_m,
            search_lin_step_m=self._patrol.match_params.search_lin_step_m,
            search_ang_rad=self._patrol.reloc_restore_yaw_rad,
            search_ang_step_rad=math.radians(1.0),
            occ_thresh=self._patrol.match_params.occ_thresh,
            min_known_cells=self._patrol.match_params.min_known_cells,
            sigma_m=self._patrol.match_params.sigma_m,
        )

    def drop_restore_anchor(self, reason: str) -> None:
        if self.restore_anchor is not None:
            LOG.info("restore_anchor_dropped", reason=reason)
        self.restore_anchor = None
        self.restore_votes.clear()

    def restore_prior(self, now_ms: int) -> Pose | None:
        """복원 창의 중심 — 기준이 아직 유효하면 (기준 자세, IMU 가 본 회전만 반영한 방위).

        상실 뒤 이동 명령이 나갔거나, 오래됐거나, IMU 가 끊겼거나 방위가 창 넘게 바뀌었으면
        (사람이 들어 돌렸을 수 있다) 기준을 버린다. 들어서 **같은 방위로** 옮긴 경우는 IMU 로
        모른다 — 그때는 창 안 점수가 전역 최고에 못 미쳐 거절되는 것에 기댄다(재생 실험).
        """
        anchor = self.restore_anchor
        if anchor is None or not self._patrol.reloc_restore_enabled:
            return None
        pose, imu0, since_ms, moves, _tilt0 = anchor
        imu = self._patrol.safety.yaw_rad
        if moves != self.move_seq:
            self.drop_restore_anchor("moved")
        elif not 0 <= now_ms - since_ms <= self._patrol.reloc_restore_max_age_ms:
            self.drop_restore_anchor("too_old")
        elif imu is None or not self._patrol.heading.imu_is_fresh(now_ms):
            self.drop_restore_anchor("imu_stale")
        elif abs(wrap_pi(imu - imu0)) > self._patrol.reloc_restore_yaw_rad:
            self.drop_restore_anchor("imu_turned")
        else:
            return (pose[0], pose[1], wrap_pi(pose[2] + wrap_pi(imu - imu0)))
        return None

    def try_restore(
        self,
        result: MatchResult | None,
        prior_result: MatchResult | None,
        points: np.ndarray,
        scan: Scan,
        now_ms: int,
    ) -> bool:
        """창 안 정합이 전역 최고에 버금가면 한 표. `reloc_votes` 표면 신뢰를 되살린다."""
        if self.restore_anchor is None:
            return False
        # 요청 뒤 결과가 오는 사이 IMU 가 돌았거나 끊겼거나 기준이 묵었을 수 있다 — 적용 직전
        # 다시 본다 (리뷰 지적). 실패·결과 없음은 연속 표를 끊는다 (P2).
        if self.restore_prior(now_ms) is None or prior_result is None or result is None:
            self.restore_votes.clear()
            return False
        grid = self._patrol.match_grid
        row, col = grid.to_cell(prior_result.pose[0], prior_result.pose[1])
        standable = (
            grid.inside(row, col) and grid.cells[row, col] <= self._patrol.plan_params.free_thresh
        )
        ratio = prior_result.score / max(1, result.score)
        if (
            prior_result.skipped
            or prior_result.score < self.min_match_score(points)
            or ratio < self._patrol.reloc_restore_score_ratio
            or not standable
        ):
            self.restore_votes.clear()
            if self._patrol._edge.changed("restore_rejected", True):
                LOG.info(
                    "restore_rejected",
                    ratio=round(ratio, 3),
                    frac=round(prior_result.score / len(points), 3),
                    standable=standable,
                )
            return False
        self._patrol._edge.changed("restore_rejected", False)
        votes = self.restore_votes
        pose = prior_result.pose
        if votes and any(
            math.hypot(pose[0] - v[0], pose[1] - v[1]) > 0.1
            or abs(wrap_pi(pose[2] - v[2])) > math.radians(3.0)
            for v in votes
        ):
            votes.clear()
        votes.append(pose)
        if len(votes) < self._patrol.reloc_votes:
            LOG.info(
                "restore_vote", n=len(votes), need=self._patrol.reloc_votes, ratio=round(ratio, 3)
            )
            return False
        self.drop_restore_anchor("restored")
        self.global_votes.clear()
        LOG.warning(
            "localization_trust_restored",
            x=round(pose[0], 2),
            y=round(pose[1], 2),
            yaw_deg=round(rad_to_deg(pose[2]), 1),
            ratio=round(ratio, 3),
            frac=round(prior_result.score / len(points), 3),
        )
        self.match_frac = prior_result.score / len(points)
        self._patrol.observe_map_pose(pose, now_ms)
        self._patrol.heading.adopt(self.result_imu)
        self.mark_verified()
        self._patrol._check_new_obstacle(scan)
        return True

    def poll_global(self, now_ms: int) -> None:
        """워커가 끝낸 결과를 루프 스레드에서 해석·적용한다 — 상태 변경은 여기서만."""
        done, prior_result = self.worker.take()
        if done is None:
            return
        kind, result, points, scan, asked_pose = done
        wall_now_ms = self._patrol.wall_clock_ms()
        asked_imu, asked_moves, asked_epoch, asked_ms = self.worker.context or (
            None,
            self.move_seq,
            self.loc_epoch,
            wall_now_ms,
        )
        if (
            asked_moves != self.move_seq
            or asked_epoch != self.loc_epoch
            or not 0 <= wall_now_ms - asked_ms <= self._patrol.global_result_max_age_ms
        ):
            # 요청 뒤에 로봇이 걸었다 — 그 스캔의 답을 지금 자세로 올리면 안 된다 (리뷰 지적).
            if self._patrol._edge.changed("global_result_stale", True):
                LOG.info("global_result_stale", kind=kind, moves=self.move_seq - asked_moves)
            if kind == "reloc":
                self.global_votes.clear()
                self.restore_votes.clear()
            return
        self._patrol._edge.changed("global_result_stale", False)
        self.result_imu = asked_imu
        if kind == "verify":
            self.apply_verify_result(result, points, now_ms, asked_pose)
        else:
            if self.try_restore(result, prior_result, points, scan, now_ms):
                self.reloc_next_ms = max(
                    self.reloc_next_ms, now_ms + self._patrol.reloc_interval_ms
                )
                return
            self.apply_reloc_result(result, points, scan, now_ms)

    def apply_reloc_result(
        self, result: MatchResult | None, points: np.ndarray, scan: Scan, now_ms: int
    ) -> None:
        """전역 재측위 결과 해석 — 요청 때의 점·스캔 기준으로 판정한다."""
        # 다음 시도 간격은 «결과가 나온 시점»부터 센다 — 탐색(~1.5초)이 간격(1초)보다
        # 길면 도착 직후 곧바로 재제출돼 국소 정합이 한 스캔도 못 도는 기아 루프가 된다.
        self.reloc_next_ms = max(self.reloc_next_ms, now_ms + self._patrol.reloc_interval_ms)
        self.match_frac = result.score / len(points) if result is not None else 0.0
        if result is None or result.score < self.min_match_score(points):
            if self._patrol._edge.changed("relocalize_failed", True):
                LOG.warning("relocalize_failed", frac=round(self.match_frac, 3))
            self.global_votes.clear()
            self.seed_fallback(points, scan, now_ms)
            return
        if result.unresolved or result.peers > self._patrol.reloc_max_peers:
            self.global_votes.clear()
            # 스캔이 지도를 구분 못 한다 — 어느 자세든 우연이다 (벽 포켓·대칭).
            if self._patrol._edge.changed("relocalize_ambiguous", True):
                LOG.warning(
                    "relocalize_ambiguous",
                    frac=round(self.match_frac, 3),
                    peers=result.peers,
                    competing_peaks=result.competing_peaks,
                    search_complete=result.search_complete,
                )
            self.seed_fallback(points, scan, now_ms)
            return
        if not self.vote_global(result.pose, result.peers):
            return
        self._patrol._edge.changed("relocalize_failed", False)
        LOG.info(
            "relocalized",
            x=round(result.pose[0], 2),
            y=round(result.pose[1], 2),
            yaw_deg=round(rad_to_deg(result.pose[2]), 1),
            frac=round(self.match_frac, 3),
            votes=self._patrol.reloc_votes,
        )
        self._patrol.observe_map_pose(result.pose, now_ms)
        # 이 자세는 요청 때 스캔의 것이다 — IMU 앵커·조향 오프셋도 그때 값으로 (이중 반영 방지).
        self._patrol.heading.adopt(self.result_imu)
        # 전역 탐색이 변별력 있게 잡은 자세다 — 여기부터의 적분은 믿을 수 있다.
        self.mark_verified()
        self._patrol._grow_map(points, result.score)
        self._patrol._check_new_obstacle(scan)

    def seed_fallback(self, points: np.ndarray, scan: Scan, now_ms: int) -> bool:
        """사람이 준 시드 주변 넓은 창으로 첫 추적을 시작한다. 잡았으면 True.

        아직 한 번도 못 잡은 경우에만, 시드가 있을 때만이다. 이렇게 잡은 자세는
        `pose_verified` 가 아니라 지도에 쓰지 않고, 서 있을 때 전역 감사가 확인한다.
        """
        if (
            not self._patrol.pose_seeded
            or self.last_pose_ms is not None
            or self._patrol.first_match_params is None
        ):
            return False
        result = match(
            self._patrol.match_grid, points, self._patrol.pose, self._patrol.first_match_params
        )
        # 사람의 말이 근거라 정합 하한은 절반만 요구한다 — 지도에 가구 다리가 빠진 자리면
        # 참 위치도 점수가 낮다. 어차피 미검증이라 지도에는 쓰지 않는다.
        if result.skipped or result.score < max(1, self.min_match_score(points) // 2):
            return False
        self.match_frac = result.score / len(points)
        LOG.info(
            "seed_tracking_started",
            x=round(result.pose[0], 2),
            y=round(result.pose[1], 2),
            frac=round(self.match_frac, 3),
            hint="전역 확인 전 — 지도에 쓰지 않음",
        )
        self._patrol.observe_map_pose(result.pose, now_ms)
        self._patrol._check_new_obstacle(scan)
        return True

    def vote_global(self, pose: Pose, peers: int = 0) -> bool:
        """전역 탐색 결과 한 표. 직전 표와 0.3m 또는 `reloc_vote_yaw_rad` 넘게 다르면 다시 센다.

        로봇이 직전 표 뒤에 움직이지 않았으면(같은 장면) 경쟁 후보가
        `reloc_stationary_max_peers` 이하인 **유일한 답**일 때만 표로 센다 — 같은 장면의
        모호한 답을 세 번 세는 것은 증거가 아니다. `reloc_votes` 표가 모이면 True 를
        돌리고 비운다 — 호출자가 그 자세를 채택한다.
        """
        votes = self.global_votes
        imu = self._patrol.safety.yaw_rad
        if (
            imu is not None
            and self.imu_at_vote is not None
            and abs(wrap_pi(imu - self.imu_at_vote)) > math.radians(5.0)
        ):
            # 명령 없이 방위가 바뀌었다 — 사람이 들어 옮긴 경우도 «움직임» 이다.
            self.moved_since_vote = True
        # **모든 표**(첫 표 기준)와 맞아야 한다 — 직전 표만 보면 0, 0.29, 0.58m 가 한 무리가 된다.
        if votes and any(
            math.hypot(pose[0] - v[0], pose[1] - v[1]) > 0.3
            or abs(wrap_pi(pose[2] - v[2])) > self._patrol.reloc_vote_yaw_rad
            for v in votes
        ):
            votes.clear()
        if votes and not self.moved_since_vote and peers > self._patrol.reloc_stationary_max_peers:
            if self._patrol._edge.changed("vote_not_independent", True):
                LOG.info("global_vote_not_independent", peers=peers)
            return False
        self._patrol._edge.changed("vote_not_independent", False)
        self.moved_since_vote = False
        self.imu_at_vote = imu
        votes.append(pose)
        if len(votes) < self._patrol.reloc_votes:
            LOG.info(
                "global_vote",
                n=len(votes),
                need=self._patrol.reloc_votes,
                x=round(pose[0], 2),
                y=round(pose[1], 2),
            )
            return False
        votes.clear()
        return True

    def mark_verified(self) -> None:
        """자세가 전역 확인됐다 — 지도 적분을 허용하고 되돌릴 기준점을 새로 찍는다."""
        self.verified = True
        self.last_verified_pose_ms = self.last_pose_ms
        if self.zone_hint is not None:
            LOG.info("zone_hint_resolved", zone=self.zone_hint[0])
            self.zone_hint = None
        if self.point_hint is not None:
            LOG.info("point_hint_resolved")
            self.point_hint = None
        self.global_votes.clear()
        self.restore_anchor = None
        self.restore_votes.clear()
        self.verified_snapshot = self._patrol.match_grid.snapshot()

    def verify_pose(self, points: np.ndarray, scan: Scan, _now_ms: int) -> bool:
        """정지 중 자세 감사 요청 — 전역 탐색을 워커로 보낸다.

        결과는 `apply_verify_result` 가 뒤늦게 적용한다. 제출에 성공했으면 True 를
        돌려 이번 스캔의 국소 정합도 건너뛴다 — 워커 기동 직후 같이 돌면 GIL 경합으로
        국소 정합이 수십 배 느려져 명령 주기를 해친다.
        """
        return self.submit_global("verify", points, scan)

    def apply_verify_result(
        self,
        result: MatchResult | None,
        points: np.ndarray,
        now_ms: int,
        asked_pose: Pose | None = None,
    ) -> bool:
        """전역 감사 결과 해석 — 다른 자리를 가리키면 자세를 바로잡고 True 를 돌린다."""
        self.match_frac = result.score / len(points) if result is not None else 0.0
        if (
            result is None
            or result.score < self.min_match_score(points)
            or result.unresolved
            or result.peers > self._patrol.reloc_max_peers
        ):
            # 구분력 없는 스캔으로는 현재 자세를 의심도 확신도 못 한다 — 유지한다.
            self.global_votes.clear()
            if self._patrol._edge.changed("pose_verify_ambiguous", True):
                LOG.warning(
                    "pose_verify_ambiguous",
                    frac=round(self.match_frac, 3),
                    peers=0 if result is None else result.peers,
                )
            return False
        self._patrol._edge.changed("pose_verify_ambiguous", False)
        # 탐색은 **요청 때의 스캔**으로 했다 — 그 사이 국소 추적이 자세를 옮겼을 수 있으니
        # 요청 때 자세와 비교한다. 그 사이 로봇이 실제로 움직였으면 이 감사는 낡았다.
        reference = self._patrol.pose if asked_pose is None else asked_pose
        drift = math.hypot(self._patrol.pose[0] - reference[0], self._patrol.pose[1] - reference[1])
        if drift > 0.1 or abs(wrap_pi(self._patrol.pose[2] - reference[2])) > math.radians(5):
            return False
        moved = math.hypot(result.pose[0] - reference[0], result.pose[1] - reference[1])
        turned = abs(wrap_pi(result.pose[2] - reference[2]))
        if moved <= 0.4 and turned <= self._patrol.reloc_vote_yaw_rad:
            # 국소 창의 잠금이 전역에서도 맞다 — 여기까지의 적분을 확정한다.
            # (신선도는 국소 추적이 계속 갱신한다 — 감사가 대신 찍지 않는다.)
            self.mark_verified()
            return False
        if self._patrol.pose_seeded and not self.verified:
            # 사람이 준 자리를 아직 전역이 한 번도 확인하지 못했다 — 지도가 덜 채워진
            # 자리(가구 다리 등)면 전역 최적 쪽이 틀릴 수 있어 **덮어쓰지 않는다.**
            # 지도에도 쓰지 않은 채 추적만 이어 가고, 트인 곳에 나오면 감사가 맞춰 준다.
            if self._patrol._edge.changed("pose_verify_disagree", True):
                LOG.warning(
                    "pose_verify_disagree",
                    seeded=[round(self._patrol.pose[0], 2), round(self._patrol.pose[1], 2)],
                    global_best=[round(result.pose[0], 2), round(result.pose[1], 2)],
                    moved_m=round(moved, 2),
                    peers=result.peers,
                    hint="시드 자리 유지 — 지도 미기록",
                )
            return False
        self._patrol._edge.changed("pose_verify_disagree", False)
        if not self.vote_global(result.pose, result.peers):
            # 한 번 다른 답이 나온 것으로는 안 옮긴다 — 같은 답이 `reloc_votes` 번 반복돼야 한다.
            return False
        LOG.warning(
            "pose_corrected",
            old=[round(self._patrol.pose[0], 2), round(self._patrol.pose[1], 2)],
            new=[round(result.pose[0], 2), round(result.pose[1], 2)],
            moved_m=round(moved, 2),
            peers=result.peers,
        )
        if self.verified_snapshot is not None:
            # 마지막 확인 뒤의 적분은 틀린 자세로 한 것이다 — 지도를 그 시점으로 되돌린다.
            self._patrol.match_grid.restore(self.verified_snapshot)
            self._patrol._rebuild_masks()
            LOG.warning("map_rolled_back", to="last_verified_snapshot")
        self._patrol.observe_map_pose(result.pose, now_ms)
        self._patrol.heading.adopt(self.result_imu)
        self.mark_verified()
        return True
