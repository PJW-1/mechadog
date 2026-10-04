"""공장 모드의 보호구 판정 — 자세 올리기·물러서기·판정 종료와 순찰 복귀 (FR-9 · ADR-42).

`ALERT` 에서 대상 트랙 하나를 판정해 적합·판정불가는 `PPE_SETTLED`, 위반은 경고 뒤
`PPE_SETTLED` 로 순찰에 돌아간다. 쓰러짐 의심(`FallMonitor.suspected`) 동안은 보류한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.behavior.commander import Commander
from host.behavior.escalation import Escalation
from host.behavior.fall_monitor import FallMonitor
from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.behavior.posture import RETURN, PostureEscalation
from host.common.logging_setup import EdgeTrigger, event_logger
from host.vision.ppe_detector import OK, UNDETERMINED, VIOLATION

if TYPE_CHECKING:
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class PpeJudge:
    """보호구 판정과 판정 자세를 맡는다.

    모든 메서드는 운용 루프 스레드에서만 부른다(`Runtime.tick`·`_apply`·시퀀스·FSM 훅).
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        escalation: Escalation,
        fall: FallMonitor,
        commander: Commander,
        apply: Callable[[Event, int], bool],
        record: Callable[[str, Any, dict[str, Any]], None],
    ) -> None:
        self._behavior = behavior
        self._mission = mission
        self._escalation = escalation
        self._fall = fall
        self._commander = commander
        self._apply = apply
        self._record = record
        self._posture = PostureEscalation(config)
        self._done: dict[int, int] = {}
        self._target: int | None = None
        self._unknown_since: int | None = None
        self._lost_since: int | None = None
        self._settle_at: int | None = None
        self._pose_held = False
        self._move_after: int | None = None
        self._now_ms = 0
        self._reverse_start: int | None = None
        self._reverse_end: int | None = None
        self._reverse_step = -float(config["gait"]["step_length_mm"])
        self._settle_ms = int(config["posture"]["settle_ms"])
        self._pitch = float(config["posture"]["pitch_up_deg"])
        self._roll = float(config["posture"].get("roll_offset_deg", 0.0))
        self._back_off_mm = float(config["posture"]["back_off_mm"])
        self._reverse_speed = (config.get("gait_calibration") or {}).get("reverse_mm_per_sec")
        self._unknown_ms = int(config["vision"]["ppe"]["violation_window_ms"])
        self._lost_ms = int(config["vision"]["tracker"]["track_lost_ms"])
        self._clear_margin = 2 * float(config["vision"]["ppe"]["head_margin_px"])
        #: 보호구 미착용 경고를 내고 순찰로 돌아가기까지 — 단계 축의 경고 시간과 같다.
        self._warning_ms = int(config["escalation"]["ppe_warning_hold_ms"])
        self._edge = EdgeTrigger()
        # 시작값은 «보류 아님» 이다. 비우면 첫 판정의 `False` 가 해제 로그로 남는다.
        self._edge.changed("ppe_held_for_fall", False)
        self._requirements: tuple[str, ...] = ("helmet", "vest")

    def set_requirements(self, required: tuple[str, ...]) -> None:
        if required != self._requirements:
            self._requirements = required
            self._done.clear()
            self._unknown_since = None

    @property
    def pose_held(self) -> bool:
        """판정 자세(고개 들기·앉기)를 쥐고 있나."""
        return self._pose_held

    @property
    def settle_ms(self) -> int:
        """자세 보간 시간(`posture.settle_ms`)."""
        return self._settle_ms

    def is_done(self, track_id: int) -> bool:
        """이 트랙의 판정을 이미 끝냈나 — 끝낸 트랙으로는 다시 멈추지 않는다."""
        return track_id in self._done

    def note_time(self, now_ms: int) -> None:
        """틱·사건 시각을 받는다. FSM 훅의 자세 복귀가 순찰 재개 시각을 잰다."""
        self._now_ms = now_ms

    def reset(self) -> None:
        """모드 전환 때 판정 기억을 지운다."""
        self._done.clear()
        self._target = None
        self._unknown_since = None
        self._lost_since = None
        self._settle_at = None
        self._move_after = None

    def settle(self, now_ms: int) -> None:
        """예약한 판정 종료 시각이 지났으면 `PPE_SETTLED` 를 낸다 (쓰러짐 의심 중에는 미룬다)."""
        if self._settle_at is not None and now_ms >= self._settle_at and not self._fall.suspected:
            self._settle_at = None
            if self._behavior.state == "ALERT" and self._apply(Event.PPE_SETTLED, now_ms):
                self._escalation.settle_ppe(now_ms)

    def forget_lost(self, result: VisionResult, now_ms: int) -> None:
        """판정한 트랙 중 보이는 것은 시각을 갱신하고 `track_lost_ms` 넘게 안 보인 것은 잊는다."""
        if self._mission.enables("ppe"):
            active = {track.track_id for track in result.tracks}
            for track_id, last_seen in tuple(self._done.items()):
                if track_id in active:
                    self._done[track_id] = now_ms
                elif now_ms - last_seen > self._lost_ms:
                    del self._done[track_id]

    def halts_patrol(self, now_ms: int) -> bool:
        """판정 자세를 푼 뒤 서기를 기다리는 중인가 — 그동안 순찰은 걷지 않는다."""
        return self._move_after is not None and now_ms < self._move_after

    def alert_sequence(self, commander: Commander, now_ms: int) -> None:
        """공장 모드 `ALERT` 의 이동 — 물러서기 구간이면 후진, 아니면 정지."""
        if self._reverse_start is not None and self._reverse_end is not None:
            if self._reverse_start <= now_ms < self._reverse_end:
                commander.drive(self._reverse_step, 0.0)
                return
            if now_ms >= self._reverse_end:
                self._reverse_start = self._reverse_end = None
        commander.drive(0.0, 0.0)

    def return_pose(self) -> bool:
        """판정 자세를 풀어 선다. 풀었으면 참 — 서는 동안 순찰 재개를 미룬다."""
        self._reverse_start = self._reverse_end = None
        held = self._pose_held
        if held:
            self._commander.once("ACTION", id=0)
            self._pose_held = False
            self._move_after = self._now_ms + max(self._settle_ms, 1000)
        return held

    def _step(self, step: str | None, now_ms: int) -> None:
        if step == "pitch_up":
            self._commander.once(
                "POSE", pitch=self._pitch, roll=self._roll, height=0.0, dur=self._settle_ms
            )
            self._pose_held = True
        elif step == "sit":
            self._commander.once("ACTION", id=1)
            self._pose_held = True
        elif step == "back_off":
            self.return_pose()
            if self._reverse_speed:
                # ACTION 0 은 온보드 loop 를 약 1초 막는다(PROTOCOL). 서기 전에 걷지 않는다.
                self._reverse_start = now_ms + max(self._settle_ms, 1000)
                self._reverse_end = self._reverse_start + int(
                    self._back_off_mm / float(self._reverse_speed) * 1000
                )
        elif step == RETURN:
            self.return_pose()

    def _finish(
        self, state: str, result: VisionResult, now_ms: int, reason: str, *, returned: bool = False
    ) -> None:
        track_id = self._target
        if track_id is None or track_id in self._done:
            return
        self._done[track_id] = now_ms
        self._unknown_since = None
        returned = self.return_pose() or returned
        event = (
            "PPE_VIOLATION"
            if state == VIOLATION
            else ("PPE_UNDETERMINED" if state == UNDETERMINED else "PPE_SETTLED")
        )
        LOG.info("ppe_judged", track_id=track_id, state=state, reason=reason)
        if state == VIOLATION:
            # 위반은 **경고**다 (S2) — 빨간 눈과 문장을 함께 내고 경고 시간 뒤에 관제 확인
            # 없이 순찰로 돌아간다. 돌아가는 길은 적합 판정과 같은 `PPE_SETTLED` 다.
            self._apply(Event.PPE_VIOLATION, now_ms)
            self._settle_at = now_ms + self._warning_ms
        elif returned:
            self._settle_at = now_ms + max(self._settle_ms, 1000)
        else:
            if self._apply(Event.PPE_SETTLED, now_ms):
                self._escalation.settle_ppe(now_ms)
        self._record(event, result, {"track_id": track_id, "state": state, "reason": reason})

    def judge(self, result: VisionResult, now_ms: int) -> None:
        """공장 모드 `ALERT` 에서 프레임 하나로 대상 트랙의 보호구를 판정한다 (ADR-42)."""
        if not self._mission.enables("ppe") or self._behavior.state != "ALERT":
            return
        if self._settle_at is not None:
            return
        # ⚠️ **쓰러졌는지 먼저 본다** (FR-11.1 표). 병행하면 위반 경고가
        # 쓰러짐보다 먼저 빨간 눈을 잡아 전용 문장이 묻히고, 적합이면 `PPE_SETTLED` 로 누운
        # 사람을 두고 순찰에 돌아간다. 의심인 동안은 판정도 자세 상승도 하지 않는다.
        # ⚠️ 보류는 후보 한 프레임이 아니라 **의심**에 건다 — 한 프레임 틈에 풀리면 이미 찬
        # 워커의 위반 창이 곧바로 경고를 낸다. 의심이 끝나면(확정 확인·상실·제한 시간) 풀린다.
        held = self._fall.suspected
        if self._edge.changed("ppe_held_for_fall", held):
            LOG.info("ppe_held_for_fall", held=held)
        if held:
            self._unknown_since = None
            self._lost_since = None
            return
        verdict = getattr(result, "ppe", None)
        if verdict is not None and verdict.required != self._requirements:
            return  # 구역 변경 직전 워커 결과는 새 정책의 판정으로 쓰지 않는다.
        if verdict is None:
            if self._lost_since is None:
                self._lost_since = now_ms
            if now_ms - self._lost_since < self._lost_ms:
                return
            decision = self._posture.update(
                box=None, frame_height=result.frame_height, track_id=self._target, now_ms=now_ms
            )
            self._step(decision.step, now_ms)
            return
        self._lost_since = None
        if verdict.track_id in self._done:
            return
        if verdict.track_id != self._target:
            self.return_pose()
            self._target = verdict.track_id
            self._unknown_since = None
        if self._reverse_start is not None:
            return
        track = next((t for t in result.tracks if t.track_id == verdict.track_id), None)
        if track is None:
            return
        if "helmet" not in verdict.required:
            returned = self.return_pose()
            if verdict.state == VIOLATION and verdict.confirmed:
                self._finish(
                    VIOLATION, result, now_ms, "구역 필수 보호구 미착용", returned=returned
                )
            elif verdict.state == OK:
                self._finish(OK, result, now_ms, verdict.reason, returned=returned)
            elif verdict.state == UNDETERMINED:
                if self._unknown_since is None:
                    self._unknown_since = now_ms
                elif now_ms - self._unknown_since >= self._unknown_ms:
                    self._finish(UNDETERMINED, result, now_ms, verdict.reason, returned=returned)
            return
        decision = self._posture.update(
            box=track.box,
            frame_height=result.frame_height,
            track_id=verdict.track_id,
            now_ms=now_ms,
        )
        returned = decision.step == RETURN and self._pose_held
        self._step(decision.step, now_ms)
        if decision.undetermined:
            self._finish(UNDETERMINED, result, now_ms, decision.reason, returned=returned)
        elif verdict.clipped or (self._pose_held and track.box[1] <= self._clear_margin):
            if decision.step == "back_off" and not self._reverse_speed:
                self._finish(UNDETERMINED, result, now_ms, "후진 속도 미실측")
            elif decision.reason == "대상이 움직이는 중 — 개시하지 않음":
                if self._unknown_since is None:
                    self._unknown_since = now_ms
                elif now_ms - self._unknown_since >= self._unknown_ms:
                    self._finish(UNDETERMINED, result, now_ms, decision.reason)
            else:
                self._unknown_since = None
        elif verdict.state == VIOLATION and verdict.confirmed:
            self._finish(VIOLATION, result, now_ms, "1500ms 창 위반 확정")
        elif verdict.state == OK:
            self._finish(OK, result, now_ms, "", returned=returned)
        elif verdict.state == UNDETERMINED:
            if self._unknown_since is None:
                self._unknown_since = now_ms
            elif now_ms - self._unknown_since >= self._unknown_ms:
                self._finish(UNDETERMINED, result, now_ms, verdict.reason, returned=returned)
