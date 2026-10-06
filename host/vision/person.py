"""사람 판정 게이트 (FR-3.2) · 쓰러짐 규칙 판정 (FR-9).

`PersonGate` 는 `person` 검출을 고정 시간 창 안의 히트 수로 모아 사람 유무를 확정한다 —
확정되면 FSM 의 `PERSON_FOUND` 가 된다 (ADR-25). 진입은 창 안 `hits_required` 회, 해제는
창이 완전히 빌 때만이다(비대칭). FSM 의 `TARGET_LOST` 는 이와 별개로 마지막 실제 검출부터
5초를 센다.

시각은 `now_ms` 로 받는다.
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
    """한 번의 관측 결과. 호출부는 `changed` 일 때만 사건을 낸다."""

    present: bool
    changed: bool
    hits: int
    best_score: float
    #: 마지막으로 사람을 실제 검출한 시각 — 대상 상실(5초)의 기준이다.
    last_seen_ms: int | None
    #: 대표 박스 — 가장 점수 높은 사람(주 대상 선정 FR-3.8.2 은 추적기 쪽이다).
    box: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class FallenVerdict:
    """쓰러짐 판정 하나 (FR-9 · ADR-35 대안 ⓓ). 호출부는 `changed` 로 엣지만 쓴다."""

    #: 종횡비·정지 조건이 `confirm_ms` 이상 이어졌다
    fallen: bool
    changed: bool
    #: 이번 프레임이 종횡비·정지 조건을 채웠다 — 쓰러짐 의심 진입 신호다 (ADR-42 결정 2 ⓐ)
    candidate: bool
    #: 가로 ÷ 세로. 박스가 없으면 `None`
    aspect: float | None
    #: 종횡비 조건이 이어진 시간. 흔들리면 0 으로 돌아간다
    still_ms: int


class FallenGate:
    """박스 종횡비(가로/세로 ≥ `aspect_ratio`)와 정지(이동 ≤ `still_threshold_px`)로 «누워
    있다» 를 판정한다 (FR-9 · ADR-35 대안 ⓓ). 추론마다 돌며 시간으로 센다.

    확정은 VLM 이 한다 (ADR-42 결정 2) — 이 판정은 의심 진입과 접근에 쓴다. 카메라 쪽으로
    누운 사람은 박스가 좁아 잡지 못한다. 정지 기준은 `vision.ppe.static_*` 와 목적이 달라
    공유하지 않는다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        section = config["vision"]["fallen"]
        self._ratio = float(section["aspect_ratio"])
        if self._ratio <= 1.0:
            raise ValueError("vision.fallen.aspect_ratio 는 1.0 보다 커야 함 (가로 > 세로)")
        self._still_px = float(section["still_threshold_px"])
        self._confirm_ms = int(section["confirm_ms"])
        if self._confirm_ms <= 0:
            raise ValueError("vision.fallen.confirm_ms 는 0 보다 커야 함")
        self._gap_ms = int(section["gap_ms"])
        if self._gap_ms < 0:
            raise ValueError("vision.fallen.gap_ms 는 0 이상이어야 함")
        self._since_ms: int | None = None
        self._last_ms: int | None = None
        self._last_centre: tuple[float, float] | None = None
        self._track_id: int | None = None
        self._fallen = False

    @property
    def confirm_ms(self) -> int:
        return self._confirm_ms

    def observe(
        self,
        now_ms: int,
        box: tuple[float, float, float, float] | None,
        *,
        track_id: int | None = None,
    ) -> FallenVerdict:
        """박스 하나를 넣고 판정을 돌려준다.

        - 박스가 `gap_ms` 이하로 빠진 틈은 누적을 이어 가고, 넘으면 지운다 — 누운 사람은
          점수가 임계 근처라 박스가 자주 빠진다.
        - 추적 ID 가 바뀌고 자리도 옮겼으면 다른 대상으로 보고 지운다 — 같은 자리면 새 ID 라도
          같은 사람이다.
        """
        if box is None:
            if self._since_ms is not None and now_ms - (self._last_ms or now_ms) <= self._gap_ms:
                return FallenVerdict(
                    fallen=self._fallen,
                    changed=False,
                    candidate=True,
                    aspect=None,
                    still_ms=max(0, now_ms - self._since_ms),
                )
            self._track_id = None
            return self._clear()
        # 프레임 자체가 끊긴 공백(스트림 재연결)도 같은 규칙이다.
        if self._last_ms is not None and now_ms - self._last_ms > self._gap_ms:
            self._clear()

        width = max(0.0, box[2] - box[0])
        height = max(0.0, box[3] - box[1])
        if height <= 0.0 or width <= 0.0:
            return self._clear()

        aspect = width / height
        centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        previous, self._last_centre = self._last_centre, centre
        # 첫 관측은 «바뀜» 이 아니다.
        switched = self._track_id is not None and track_id != self._track_id
        self._track_id = track_id
        moved = (
            0.0
            if previous is None
            else max(abs(centre[0] - previous[0]), abs(centre[1] - previous[1]))
        )

        if switched and (previous is None or moved > self._still_px):
            return self._clear(aspect=aspect)
        if aspect < self._ratio or moved > self._still_px:
            verdict = self._clear(aspect=aspect)
            self._last_centre = centre
            return verdict

        if self._since_ms is None:
            self._since_ms = now_ms
        self._last_ms = now_ms
        still_ms = max(0, now_ms - self._since_ms)
        fallen = still_ms >= self._confirm_ms
        changed = fallen != self._fallen
        self._fallen = fallen
        if changed and fallen:
            LOG.warning("person_fallen", aspect=round(aspect, 2), still_ms=still_ms, track=track_id)
        return FallenVerdict(
            fallen=fallen, changed=changed, candidate=True, aspect=aspect, still_ms=still_ms
        )

    def reset(self) -> None:
        """스트림이 끊겼을 때 호출부가 지운다 (`PersonGate.reset` 과 같은 자리)."""
        self._track_id = None
        self._clear()

    @property
    def aspect_ratio(self) -> float:
        return self._ratio

    def _clear(self, *, aspect: float | None = None) -> FallenVerdict:
        changed = self._fallen
        self._fallen = False
        self._since_ms = None
        self._last_ms = None
        self._last_centre = None
        return FallenVerdict(
            fallen=False, changed=changed, candidate=False, aspect=aspect, still_ms=0
        )


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

        사람이 안 보인 관측도 넣어야 한다 — 그래야 창이 비어 확정이 풀린다.
        """
        was = self._present
        # 관측 공백이 창보다 길면(스트림 복구) 이전 히트를 버리고 새 창을 시작한다.
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
            # 해제는 창이 완전히 빌 때만이다.
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
        """창 밖으로 나간 관측을 버린다. 경계는 포함이다."""
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
