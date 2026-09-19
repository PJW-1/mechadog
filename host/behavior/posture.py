"""단계적 자세 상승 — 머리가 잘린 프레임에서 시야를 되찾는다 (WBS 3.5.7 · FR-9.2.2).

카메라가 **15cm 높이**에 있어서 사람이 가까이 서면 머리가 화각 위로 잘린다. 그러면
안전모를 볼 수 없고 PPE 판정이 성립하지 않는다(FR-9.2.1). 거리를 재서 푸는 것이
아니라(DR-15 — 잴 수단이 없다) **자세를 올려** 푼다.

    ① pitch_up   POSE  설정 각도        시선 상향
    ② sit        ACTION 1 `sit_dowm`   상체 상승 + 시선 크게 상향
    ③ back_off   MOVE  후진             전신을 화각 안으로

⚠️ **매 단계마다 다시 본다.** 한 단계로 풀리면 거기서 멈춘다 — 단계는 비용이 다르고
(③ 은 움직인다) 필요 이상으로 올리면 그만큼 느려지고 위험해진다.

⚠️ **대상이 정지해 있을 때만 개시한다 (FR-9.2.0).** 로봇 보행은 10~30cm/s 이고 사람은
120~150cm/s 다. 움직이는 대상에 자세를 올리면 `자세 → 이탈 → 복귀 → 이동 → 재클리핑`
이 **한 바퀴마다 대상이 더 멀어지는 루프**가 된다. 산업안전 점검의 대상은 *"작업 위치에
있는 작업자"* 이지 걸어가는 사람이 아니다.

⚠️ **이 모듈은 명령을 보내지 않는다.** 무엇을 할지만 정하고 전송은 부르는 쪽이 한다 —
`tracker.LockOnTracker` 와 같은 모양이며, 그래서 로봇 없이 시험이 닫힌다.

⚠️ **시계를 만들지 않는다.** `now_ms` 를 받는다. 자세 보간(`settle_ms`)이 끝나기 전에
다시 보면 «아직 안 풀렸다» 가 나와 단계를 헛되이 올린다.

거리 유지(FR-3.5.2)는 여기 없다 — 그쪽은 `3.5.8` 이고 bbox **높이**로 하는 별개 제어다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["PostureDecision", "PostureEscalation"]

#: 설정 `posture.escalation_steps` 가 쓰는 이름. 순서가 곧 비용 순서다.
STEP_PITCH_UP = "pitch_up"
STEP_SIT = "sit"
STEP_BACK_OFF = "back_off"
KNOWN_STEPS = (STEP_PITCH_UP, STEP_SIT, STEP_BACK_OFF)

#: 기본 자세 복귀. 단계 이름이 아니라 **되돌리기**이며 `ACTION 0 stand_four_legs` 다.
RETURN = "return"


@dataclass(frozen=True, slots=True)
class PostureDecision:
    """이번 프레임에 할 일.

    `step` 이 `None` 이면 **아무것도 하지 않는다** — 이미 보낸 것을 되풀이하지 않는다.
    """

    step: str | None
    reason: str
    #: 재시도를 다 쓰고 판정을 포기했나 (FR-9.2.4). 부르는 쪽이 `PPE_UNDETERMINED` 를
    #: 기록하고 순찰로 돌아간다. **판정 실패도 결과다** — 적지 않으면 고장으로 보인다.
    undetermined: bool = False


class PostureEscalation:
    """클리핑 관측을 단계로 바꾼다. 상태를 들고 있으므로 개체당 하나다."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        posture = config["posture"]
        ppe = config["vision"]["ppe"]
        steps = tuple(str(s) for s in posture["escalation_steps"])
        if not steps:
            raise ValueError("posture.escalation_steps 가 비어 있음")
        unknown = [s for s in steps if s not in KNOWN_STEPS]
        if unknown:
            raise ValueError(f"모르는 자세 단계: {unknown}")
        self._steps: Sequence[str] = steps
        self._settle_ms = int(posture["settle_ms"])
        if self._settle_ms <= 0:
            raise ValueError("posture.settle_ms 는 0 보다 커야 함")
        self._abort_on_lost = bool(posture["abort_on_target_lost"])
        self._head_margin_px = float(ppe["head_margin_px"])
        self._static_px = float(ppe["static_threshold_px"])
        self._static_frames = int(ppe["static_frames"])
        if self._static_frames <= 0:
            raise ValueError("vision.ppe.static_frames 는 1 이상이어야 함")
        self._max_retries = int(ppe["max_posture_retries"])

        #: 지금 몇 번째 단계를 **보냈나**. `-1` 은 아직 아무것도 안 보냈다는 뜻이다.
        self._sent_index = -1
        self._sent_at_ms: int | None = None
        #: 지금 쫓는 추적 ID. 바뀌면 시퀀스도 재시도 횟수도 새로 센다 — 다른 사람이다.
        self._track_id: int | None = None
        self._retries = 0
        #: 정지 판정용. 중심 x·y 와 «연속으로 조용했던 프레임 수».
        self._last_centre: tuple[float, float] | None = None
        self._still_frames = 0

    @property
    def step(self) -> str | None:
        """지금 잡고 있는 단계. 아무것도 안 잡았으면 `None`."""
        if self._sent_index < 0:
            return None
        return self._steps[self._sent_index]

    @property
    def retries(self) -> int:
        return self._retries

    def _reset(self) -> None:
        self._sent_index = -1
        self._sent_at_ms = None
        self._last_centre = None
        self._still_frames = 0

    def _abort(self, reason: str) -> PostureDecision:
        """중단하고 기본 자세로 돌아간다. 재시도를 한 번 쓴다 (FR-9.2.4)."""
        held = self._sent_index >= 0
        self._reset()
        if not held:
            # 잡은 적이 없으면 되돌릴 것도 없다. 헛된 `ACTION` 은 1초를 블로킹한다.
            return PostureDecision(None, reason)
        self._retries += 1
        return PostureDecision(RETURN, reason)

    def _observe_still(self, centre: tuple[float, float]) -> bool:
        """대상이 정지해 있나 (FR-9.2.0). 중심 이동량이 임계 이하로 이어졌는가."""
        previous = self._last_centre
        self._last_centre = centre
        if previous is None:
            self._still_frames = 0
            return False
        moved = max(abs(centre[0] - previous[0]), abs(centre[1] - previous[1]))
        if moved > self._static_px:
            self._still_frames = 0
            return False
        self._still_frames += 1
        return self._still_frames >= self._static_frames

    def update(
        self,
        *,
        box: tuple[float, float, float, float] | None,
        frame_height: int,
        track_id: int | None,
        now_ms: int,
    ) -> PostureDecision:
        """한 프레임의 관측으로 이번에 할 일을 정한다.

        `box` 는 **사람** bbox `[x1, y1, x2, y2]` 이며 `None` 이면 대상이 안 보인다.
        클리핑 판정은 `y1` 이 프레임 위 경계에 닿았는지로 한다 (FR-9.2.1).
        """
        if frame_height <= 0:
            raise ValueError("frame_height 는 1 이상이어야 함")

        if track_id != self._track_id:
            # **다른 사람이다.** 재시도 횟수까지 새로 센다 — 앞사람 몫을 물려받으면
            # 뒷사람이 시도도 못 해보고 미판정이 된다.
            self._track_id = track_id
            self._retries = 0
            self._reset()

        if box is None:
            if not self._abort_on_lost:
                return PostureDecision(None, "대상 미검출 — 중단하지 않는 설정")
            return self._abort("대상이 화각을 벗어남")

        centre = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        clipped = float(box[1]) <= self._head_margin_px
        still = self._observe_still(centre)

        if not clipped:
            # 시야가 확보됐다. 잡고 있던 자세가 있으면 되돌린다 — **되돌리기는
            # 재시도로 세지 않는다.** 성공이지 실패가 아니다.
            if self._sent_index < 0:
                return PostureDecision(None, "클리핑 없음")
            self._reset()
            return PostureDecision(RETURN, "시야 확보 — 기본 자세로")

        if not still:
            # ⚠️ **움직이는 대상에는 개시하지 않는다.** 이미 올라가 있으면 그대로 둔다 —
            # 내렸다 올렸다 하는 것이 루프의 시작이다.
            return PostureDecision(None, "대상이 움직이는 중 — 개시하지 않음")

        if self._sent_at_ms is not None and now_ms - self._sent_at_ms < self._settle_ms:
            # 보간이 끝나기 전에 다시 보면 «아직 잘린다» 가 나와 단계를 헛되이 올린다.
            return PostureDecision(None, "자세가 도착하기를 기다리는 중")

        nxt = self._sent_index + 1
        if nxt >= len(self._steps):
            # 단계를 다 썼는데도 잘린다. 한 번의 실패로 치고 되돌린다.
            decision = self._abort("단계를 모두 썼는데도 클리핑")
            if self._retries > self._max_retries:
                return PostureDecision(decision.step, decision.reason, undetermined=True)
            return decision

        if self._retries > self._max_retries:
            return PostureDecision(None, "재시도 한도 초과", undetermined=True)

        self._sent_index = nxt
        self._sent_at_ms = now_ms
        return PostureDecision(self._steps[nxt], f"클리핑 — {self._steps[nxt]}")
