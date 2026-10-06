"""공장 쓰러짐 감시: 의심은 후보·VLM, 확정은 정지 규칙 + 같은 프레임의 VLM yes.

불일치·실패·시간초과는 확인 필요 사건이다. PPE 시험에서는 이동·경보 전이를 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.behavior.escalation import Escalation
from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.common.logging_setup import event_logger
from host.vision.vlm_worker import VlmWorker

if TYPE_CHECKING:
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class FallMonitor:
    """쓰러짐 의심·확정·해제와 쓰러짐 판독을 맡는다.

    모든 메서드는 운용 루프 스레드에서만 부른다(`Runtime._poll_vision`·`confirm_alarm`).
    판독 워커는 구역 판독과 하나를 나눠 쓴다 — `zone_waiting` 이 참이면 걸지 않는다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        escalation: Escalation,
        vlm: VlmWorker,
        apply: Callable[[Event, int], bool],
        record: Callable[[str, Any, dict[str, Any]], None],
        zone_waiting: Callable[[], bool],
    ) -> None:
        fsm = config["fsm"]
        self._behavior = behavior
        self._mission = mission
        self._escalation = escalation
        self._vlm = vlm
        self._apply = apply
        self._record = record
        self._zone_waiting = zone_waiting
        #: 의심에 든 시각. `None` 이면 의심이 아니다. PPE 판정은 이 동안 보류한다.
        self._since: int | None = None
        #: 의심 중 VLM yes 진단 횟수. 확정 근거로 사용하지 않는다.
        self._yes = 0
        #: 마지막으로 센 «예» — 판독으로 의심에 들었으면 그 판독 — 를 건 시각. 다음 «예» 는
        #: 이것과 `fsm.fall_confirm_gap_ms` 이상 떨어진 프레임이어야 센다.
        self._yes_asked: int | None = None
        #: 이번 의심을 확정했나. 확정했으면 관제 확인(`confirm_alarm`)이 순찰로 돌려보낸다.
        self._confirmed = False
        #: 돌고 있는 쓰러짐 판독 `(건 시각, 건 프레임, 의심 중에 걸었나)`.
        self._pending: tuple[int, Any, bool] | None = None
        #: 다음 순찰 판독 시각. 순찰이 아니면 `None` 이다.
        self._due: int | None = None
        self._gap_ms = int(fsm["fall_confirm_gap_ms"])
        self._timeout_ms = int(fsm["fall_suspect_timeout_ms"])
        #: 제한 시간 초과·경보 확인으로 순찰에 돌아간 뒤 다시 의심하지 않는 시간과 끝 시각.
        self._cooldown_ms = int(fsm["fall_resuspect_cooldown_ms"])
        self._cooldown_until: int | None = None
        self._patrol_vlm_ms = int(config["vision"]["vlm"]["patrol_interval_ms"])
        self._test_mode = bool(config["vision"]["ppe"].get("test_mode", False))
        self._cross_budget = int(config["vision"]["vlm"]["budget_ms"])
        self._cross_pending: tuple[int, Any] | None = None
        self._cross_reported = False
        self._rule_seen = False
        self._rule_wait_since: int | None = None
        self._rule_wait_frame: Any = None

    @property
    def suspected(self) -> bool:
        """의심 중인가 — 확정한 뒤 관제가 확인할 때까지도 참이다."""
        return self._since is not None

    @property
    def confirmed(self) -> bool:
        """이번 의심을 확정했나(`PERSON_DOWN` 을 냈나)."""
        return self._confirmed

    @property
    def waiting(self) -> bool:
        """쓰러짐 판독이 워커에서 돌고 있나. 구역 판독은 이것이 참이면 걸지 않는다."""
        return self._pending is not None or self._cross_pending is not None

    def suspect(self, source: str, now_ms: int, *, yes_asked: int | None = None) -> None:
        """쓰러짐 의심에 든다 — 순찰·구역 점검이면 `FALL_SUSPECTED`, 추종 중이면 그 자리에서.

        `yes_asked` 는 의심에 들게 한 판독을 건 시각이다 — 확정에 세지 않고 다음 «예» 의
        간격만 잰다. 재의심 쿨다운 안이면 들지 않는다.
        """
        if self._test_mode or self._since is not None or not self._mission.enables("fallen"):
            return
        # 순찰로 돌려보낸 직후에는 다시 의심하지 않는다 — 누운 가방 같은 헛검출 앞에서
        # 노랑·파랑을 되풀이하며 순찰을 잇지 못한다.
        if self._cooldown_until is not None and now_ms < self._cooldown_until:
            return
        if not (self._behavior.tracking or self._apply(Event.FALL_SUSPECTED, now_ms)):
            return
        self._since = now_ms
        self._yes = 0
        self._yes_asked = yes_asked
        self._confirmed = False
        # 5초 상실은 의심에 든 때부터 센다 — 판독으로만 든 의심에는 검출 시각이 없다.
        self._behavior.note_target(now_ms)
        self._escalation.note_fall_suspect(True, now_ms)
        LOG.warning("fall_suspected", source=source)

    def watch(self, now_ms: int) -> None:
        """의심을 끝낼 때를 본다. 틱마다 — 새 프레임이 없어도 — 부른다.

        `ALERT`·`TRACK` 을 떠났으면 쿨다운 없이 끝내고, 확정하지 못한 채
        `fsm.fall_suspect_timeout_ms` 가 지나면 순찰로 돌려보낸다.
        """
        self.take_cross_reading(now_ms)
        if (
            self._rule_wait_since is not None
            and not self._rule_seen
            and self._rule_wait_frame is not None
        ):
            elapsed = now_ms - self._rule_wait_since
            if elapsed >= self._cross_budget:
                self._cross_result(
                    self._rule_wait_frame, None, elapsed, "vlm_unavailable_or_busy", now_ms=now_ms
                )
                self._rule_seen = True
        if self._since is None:
            return
        if not self._behavior.tracking:
            self._end(now_ms)
        elif not self._confirmed and now_ms - self._since >= self._timeout_ms:
            LOG.info("fall_suspect_timeout", vlm_yes=self._yes)
            self.resolve(now_ms)

    def resolve(self, now_ms: int) -> None:
        """`FALL_RESOLVED` 로 순찰에 돌려보내고 재의심 쿨다운을 건다 — 제한 시간·경보 확인."""
        self._apply(Event.FALL_RESOLVED, now_ms)
        self._end(now_ms)
        self._cooldown_until = now_ms + self._cooldown_ms

    def _end(self, now_ms: int) -> None:
        self._since = None
        self._yes = 0
        self._yes_asked = None
        self._confirmed = False
        self._escalation.note_fall_suspect(False, now_ms)

    def take_reading(self, now_ms: int) -> None:
        """끝난 의심 판독을 줍는다. yes는 의심 진입·유지에만 사용한다.

        의심 중에 건 판독이 의심이 끝난 뒤에 오면 버리고, 앞서 센 «예» 와
        `fsm.fall_confirm_gap_ms` 안에 건 «예» 는 세지 않는다 (ADR-42).
        """
        self.take_cross_reading(now_ms)
        if self._cross_pending is not None or self._pending is None or self._vlm.busy:
            return
        (asked_ms, _asked, during), self._pending = self._pending, None
        reading = self._vlm.take()
        if reading is None:
            return  # 스레드가 죽었다 — 사유는 `vlm_worker_failed` 가 남겼다
        down = reading.get("person_down")
        LOG.info("fall_reading", degraded=reading.degraded, reason=reading.reason, person_down=down)
        if not down:
            return
        if self._since is None:
            if not during:
                self.suspect("vlm", now_ms, yes_asked=asked_ms)
        elif asked_ms >= self._since:
            # «예» 는 대상을 본 것이다 — 박스 없이 판독으로만 보는 동안 5초 상실로 풀지 않는다.
            self._behavior.note_target(now_ms)
            last = self._yes_asked
            if last is not None and asked_ms - last < self._gap_ms:
                return
            self._yes += 1
            self._yes_asked = asked_ms
            # 확정은 규칙 확정 프레임의 교차검증 경로에서만 한다.

    def ask(self, result: VisionResult, now_ms: int) -> None:
        """`person_down` 하나만 묻는 판독을 건다 (ADR-35) — 순찰 중에는
        `vision.vlm.patrol_interval_ms` 마다, 확정 전 의심 중에는 판독이 끝날 때마다.

        쓰러짐·구역 판독 어느 쪽이든 돌고 있으면 걸지 않는다 — 워커의 결과 슬롯은 하나다.
        """
        if not self._mission.enables("fallen"):
            return
        rule = getattr(result, "fallen", None)
        if rule is not None and rule.fallen:
            self._ask_cross(result, now_ms)
            return
        if self._cross_pending is not None:
            return
        self._rule_seen = False
        self._rule_wait_since = None
        self._rule_wait_frame = None
        if self._test_mode:
            return
        if self._since is not None:
            self._due = None
            if self._confirmed:
                return  # 확정했으면 더 물을 것이 없다
        elif self._behavior.state == "PATROL":
            if self._due is None:
                self._due = now_ms + self._patrol_vlm_ms
            if now_ms < self._due:
                return
            self._due = now_ms + self._patrol_vlm_ms
        else:
            self._due = None
            return
        if self._pending is not None or self._zone_waiting():
            return
        if self._vlm.submit(result.jpeg, now_ms=now_ms, keys=("person_down",)):
            self._pending = (now_ms, result, self._since is not None)

    def _ask_cross(self, result: VisionResult, now_ms: int) -> None:
        """규칙 확정의 같은 JPEG를 묻는다. 정지·FAILSAFE도 상태 전이 없이 허용한다."""
        if self._rule_seen or self._cross_pending is not None:
            return
        if self._rule_wait_since is None:
            self._rule_wait_since = now_ms
            self._rule_wait_frame = result
        if now_ms - self._rule_wait_since >= self._cross_budget:
            self._cross_result(
                self._rule_wait_frame,
                None,
                now_ms - self._rule_wait_since,
                "vlm_unavailable_or_busy",
                now_ms=now_ms,
            )
            self._rule_seen = True
            return
        if self._pending is not None or self._zone_waiting():
            return
        frame = self._rule_wait_frame
        if self._vlm.submit(frame.jpeg, now_ms=now_ms, keys=("person_down",)):
            self._cross_pending = (now_ms, frame)
            self._cross_reported = False
            self._rule_seen = True
            LOG.info("fall_cross_requested", frame_seq=frame.frame_seq, test_mode=self._test_mode)

    def take_cross_reading(self, now_ms: int) -> None:
        if self._cross_pending is None:
            return
        asked_ms, frame = self._cross_pending
        elapsed = max(0, now_ms - asked_ms)
        if self._vlm.busy:
            if elapsed >= self._cross_budget and not self._cross_reported:
                self._cross_result(frame, None, elapsed, "timeout", now_ms=now_ms)
                self._cross_reported = True
            # 결과 슬롯의 소유권은 늦은 응답을 버릴 때까지 유지한다.
            return
        reading = self._vlm.take()
        self._cross_pending = None
        if self._cross_reported:
            return
        value = reading.get("person_down") if reading is not None else None
        reason = reading.reason if reading is not None else "worker_failed"
        if elapsed >= self._cross_budget or (reading is not None and reading.degraded):
            value = None
            reason = reason or "timeout"
        raw = (
            next((a.raw for a in reading.answers if a.key == "person_down"), "") if reading else ""
        )
        reason = reason or (
            "agreement" if value is True else "disagreement" if value is False else "undetermined"
        )
        self._cross_result(frame, value, elapsed, reason, raw, now_ms=now_ms)

    def _cross_result(
        self,
        frame: Any,
        value: bool | None,
        latency: int,
        reason: str,
        raw: str = "",
        *,
        now_ms: int,
    ) -> None:
        if not self._mission.enables("fallen"):
            return
        confirmed = value is True
        judgement = {
            "fallen": confirmed,
            "rule_yes": True,
            "aspect": frame.fallen.aspect,
            "still_ms": frame.fallen.still_ms,
            "vlm": value,
            "latency_ms": latency,
            "reason": reason,
            "raw": raw,
            "test_mode": self._test_mode,
            "status": "확정" if confirmed else "확인 필요",
            "person_down_reference": [
                {"score": d.score, "box": list(d.box)}
                for d in getattr(frame, "person_down_reference", ())
            ],
            "reference_reason": getattr(frame, "person_down_reference_reason", "disabled"),
        }
        LOG.info("fall_cross_result", **judgement)
        self._record("person_fallen" if confirmed else "fall_review_required", frame, judgement)
        if confirmed and not self._test_mode:
            self._confirmed = True
            self._apply(Event.PERSON_DOWN, now_ms)
