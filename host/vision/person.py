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


@dataclass(frozen=True, slots=True)
class FallenVerdict:
    """쓰러짐 판정 하나 (WBS 4.8.3 · FR-9 · ADR-35 대안 ⓓ).

    `changed` 를 따로 두는 이유는 `Sighting` 과 같다 — 쓰러진 사람은 **계속** 쓰러져
    있으므로, 매 프레임 참을 그대로 올리면 같은 사건이 25fps 로 쏟아진다.
    """

    #: 확정. 종횡비와 정지 지속을 **둘 다** 채웠다
    fallen: bool
    changed: bool
    #: 종횡비 조건만 채운 상태. 대시보드가 «지켜보는 중» 을 보여줄 자리다
    candidate: bool
    #: 가로 ÷ 세로. 박스가 없으면 `None`
    aspect: float | None
    #: 종횡비 조건이 이어진 시간. 흔들리면 0 으로 돌아간다
    still_ms: int


class FallenGate:
    """박스 모양과 정지 지속으로 «누워 있다» 를 판정한다 (WBS 4.8.3 · FR-9).

    **VLM 과 함께 둔다** (ADR-35 대안 ⓓ). 즉시·무료·설명 가능하며, 가장 급한 사건을
    0.4초짜리 모델 하나에만 맡길 이유가 없다. VLM 은 구역당 한 번이지만 이쪽은
    추론마다 돈다.

    실측 근거 — 쓰러진 작업자를 YOLOX 가 `person` 0.89 로 잡았고 박스는 **세로 88 ·
    가로 286**(종횡비 3.25)이었다. 서 있는 사람은 0.4 안팎, 앉은 사람도 1.0 을 크게
    넘지 않는다.

    ⚠️ **종횡비만으로 확정하지 않는다.** 카메라에 바싹 붙은 사람, 두 사람이 겹쳐
    잡힌 박스, 팔을 벌린 순간도 가로로 넓다. 15cm 저각이라 더 그렇다. 그래서
    **정지가 이어질 때만** 확정한다 — 넘어진 사람은 움직이지 않는다.

    ⚠️ **프레임이 아니라 시간으로 센다** (ADR-25). 프레임 수로 두면 의미가 추론률에
    종속되어 같은 조건이 10fps 와 25fps 에서 2.5배 다른 시간이 된다.

    ⚠️ **`vision.ppe.static_*` 를 빌려 쓰지 않는다.** 저쪽은 *"자세를 올려도 되나"*
    (FR-9.2.0)이고 이쪽은 *"위험한가"* 다. 저쪽은 곧 움직일 사람만 거르면 되어 0.2초면
    충분하지만, 넘어진 직후 버둥거리는 것과 의식을 잃은 것을 가르려면 훨씬 길어야
    한다. 우연히 같은 숫자를 공유하면 한쪽을 고칠 때 다른 쪽이 딸려 간다.

    ⚠️ **카메라 쪽으로 누우면 못 잡는다.** 머리-발 축이 광축과 나란하면 박스가 오히려
    좁아진다. 그래서 이것이 유일한 경로가 아니라 VLM 과 **둘** 인 것이다.
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

        ⚠️ **박스가 `gap_ms` 넘게 없으면 누적을 지운다.** 남기면 사람이 사라졌다 다시
        나타났을 때 이전 관측이 이번 확정을 채워 준다 — 한 번 보고 확정하는 꼴이 된다.
        그보다 짧은 빈 틈은 봐준다: 누운 사람은 점수가 임계값 근처라 박스가 자주 빠지고,
        한 프레임에 지우면 3초를 끊김 없이 채우지 못한다 (2026-09-23 실기).

        ⚠️ **다른 자리의 대상이면 지운다.** 다른 사람의 정지가 이번 사람 몫을 채우면
        안 된다. 추적 ID 만 보지 않는 이유는 추적기가 검출이 1초 빠지면 **같은 사람에게
        새 ID 를 주기** 때문이다 — 자리가 같으면(`still_threshold_px`) 같은 사람이다.
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
        # 프레임 자체가 끊긴 공백(스트림 재연결)도 같은 규칙이다 — 그동안은 `None` 조차
        # 들어오지 않는다.
        if self._last_ms is not None and now_ms - self._last_ms > self._gap_ms:
            self._clear()

        width = max(0.0, box[2] - box[0])
        height = max(0.0, box[3] - box[1])
        if height <= 0.0 or width <= 0.0:
            return self._clear()

        aspect = width / height
        centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        previous, self._last_centre = self._last_centre, centre
        # ⚠️ **첫 관측은 «바뀜» 이 아니다.** 시작값과 견주면 어떤 대상이 들어와도
        # 한 번은 버려져, 추적 ID 가 붙어 있는 한 영원히 확정되지 않는다.
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
