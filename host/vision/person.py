"""사람 판정 게이트 (WBS 3.3.3 · FR-3.2).

검출 목록에서 `person` 만 걸러 **시간 창 안의 검출 횟수**로 사람 유무를 확정한다.
확정되면 FSM 의 `PERSON_FOUND` 사건이 된다.

⚠️ **"연속 N프레임" 대신 고정 시간 창 안의 히트 수를 센다.** 프레임 수만 정의하면
허용되는 관측 기간까지 추론률에 따라 달라진다. 시간 창은 최대 관측 간격을 고정하지만,
확인 지연과 히트 기회는 여전히 추론률의 영향을 받는다. 그래서 25fps는 실측값으로 고정하고
바꿀 때 검출률과 오검출률을 다시 잰다.

실측이 그것을 드러냈다 (2026-09-10 · 실기 343프레임 · 사람이 걸어서 통과) —
`inference_fps: 10` 에서 **진짜 검출 구간 10개 중 4개만** 조건을 채웠다. 25fps 에서는
10개 전부 인정되고 단발 8개가 전부 막혔으며 **경계의 2연속이 0개**였다. 짧은 구간
(120~240ms)이 건너뛰기에 사라지기 때문이다.

⚠️ **진입과 해제를 대칭으로 두지 않는다.** 진입은 창 안 `hits_required` 회, 해제는
**창이 완전히 빌 때**만이다. 대칭으로 두면 게이트가 경계에서 떨려 후속 추적 판단이
흔들린다. FSM의 `TARGET_LOST`는 이 해제와 별개로 마지막 실제 검출부터 5초를 센다.

⚠️ **이 모듈은 시간을 만들지 않는다.** `now_ms` 를 받으므로 가상 시간으로 전수 검증된다.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from host.common.logging_setup import event_logger
from host.vision.detector import Detection

LOG = event_logger("mechadog.vision")


@dataclass(frozen=True, slots=True)
class Sighting:
    """한 번의 관측 결과.

    `changed` 를 따로 두는 이유 — 호출부가 **바뀐 순간에만** 사건을 발행해야 한다.
    같은 값이 20Hz 로 반복되는데 매번 발행하면 FSM 이 같은 전이를 수백 번 본다.
    """

    present: bool
    changed: bool
    hits: int
    best_score: float
    #: 마지막으로 사람을 실제 검출한 시각. 게이트 확정이 풀리는 300ms와 FSM의
    #: 대상 상실 5초를 분리할 때 사용한다.
    last_seen_ms: int | None
    #: 대표 박스 — 가장 점수 높은 사람. ⚠️ **주 대상 선정(FR-3.8.2, 최근접)은 여기가
    #: 아니다** — 그것은 추적기(`3.3.4`) 위에서 정해진다.
    box: tuple[float, float, float, float] | None


class PersonGate:
    """`person` 검출을 시간 창으로 모아 유무를 확정한다."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        vision = config["vision"]
        self._window_ms = int(vision["detect_window_ms"])
        self._required = int(vision["detect_hits_required"])
        self._label = str(vision["coco"]["person_class"])
        #: (관측 시각, 그 관측에서 사람이 보였나)
        self._seen: deque[tuple[int, bool]] = deque()
        self._present = False
        self._last: Sighting | None = None
        self._last_observed_ms: int | None = None
        self._last_seen_ms: int | None = None

    @property
    def window_ms(self) -> int:
        return self._window_ms

    @property
    def hits_required(self) -> int:
        return self._required

    def observe(self, now_ms: int, detections: Sequence[Detection]) -> Sighting:
        """관측 하나를 넣고 현재 판정을 돌려준다.

        ⚠️ **사람이 안 보인 관측도 반드시 넣어야 한다.** 넣지 않으면 창이 오래된
        히트만 담은 채로 남아 사람이 사라진 뒤에도 확정이 유지된다.
        """
        was = self._present
        # 스트림이 끊겼다 돌아오면 이전 히트로 즉시 재확정하면 안 된다. 관측 공백이
        # 창보다 길면 첫 복구 프레임부터 새 창을 시작한다.
        if self._last_observed_ms is not None and now_ms - self._last_observed_ms > self._window_ms:
            self._seen.clear()
            self._present = False
            self._last_seen_ms = None
        self._last_observed_ms = now_ms

        people = [d for d in detections if d.label == self._label]
        if people:
            self._last_seen_ms = now_ms
        self._seen.append((now_ms, bool(people)))
        self._prune(now_ms)

        hits = sum(1 for _at, seen in self._seen if seen)
        if not self._present:
            self._present = hits >= self._required
        elif hits == 0:
            # 해제는 창이 **완전히** 빌 때만 — 진입과 대칭이면 경계에서 떨린다.
            self._present = False

        best = max(people, key=lambda d: d.score) if people else None
        sighting = Sighting(
            present=self._present,
            changed=self._present != was,
            hits=hits,
            best_score=best.score if best else 0.0,
            last_seen_ms=self._last_seen_ms,
            box=best.box if best else None,
        )
        if sighting.changed:
            LOG.info(
                "person_present" if self._present else "person_cleared",
                hits=hits,
                required=self._required,
                window_ms=self._window_ms,
                score=round(sighting.best_score, 3),
            )
        self._last = sighting
        return sighting

    def _prune(self, now_ms: int) -> None:
        """창 밖으로 나간 관측을 버린다. 경계는 **포함**이다(`>` 로 자른다)."""
        cutoff = now_ms - self._window_ms
        while self._seen and self._seen[0][0] < cutoff:
            self._seen.popleft()

    @property
    def present(self) -> bool:
        return self._present

    @property
    def last(self) -> Sighting | None:
        return self._last

    def reset(self) -> None:
        """상태를 비운다. 스트림이 끊겼다 붙으면 이전 창을 이어 쓰지 않는다."""
        self._seen.clear()
        self._present = False
        self._last = None
        self._last_observed_ms = None
        self._last_seen_ms = None
