"""공장 모드의 쓰러짐 감시 — «의심(L1) → 확정(L3)» 2단 (ADR-35 · ADR-42).

의심은 누움 후보·판독 «예» 한 번으로 들고, 확정은 의심 뒤에 건 판독의 «예» 가
`fsm.fall_confirm_vlm_yes` 회, 서로 `fsm.fall_confirm_gap_ms` 이상 떨어진 프레임이어야
한다. 규칙(YOLOX 누움)만으로는 L3 를 내지 않는다.
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
        #: 의심 뒤에 건 판독의 «예» 를 센 횟수. 끊겨도 누적한다.
        self._yes = 0
        #: 마지막으로 센 «예» — 판독으로 의심에 들었으면 그 판독 — 를 건 시각. 다음 «예» 는
        #: 이것과 `fsm.fall_confirm_gap_ms` 이상 떨어진 프레임이어야 센다.
        self._yes_asked: int | None = None
        #: 의심 뒤에 건 판독이 «예» 라 한 원문. 확정 기록에 싣는다.
        self._vlm_yes: str | None = None
        #: 이번 의심을 확정했나. 확정했으면 관제 확인(`confirm_alarm`)이 순찰로 돌려보낸다.
        self._confirmed = False
        #: 돌고 있는 쓰러짐 판독 `(건 시각, 건 프레임, 의심 중에 걸었나)`.
        self._pending: tuple[int, Any, bool] | None = None
        #: 다음 순찰 판독 시각. 순찰이 아니면 `None` 이다.
        self._due: int | None = None
        self._need = int(fsm["fall_confirm_vlm_yes"])
        self._gap_ms = int(fsm["fall_confirm_gap_ms"])
        self._timeout_ms = int(fsm["fall_suspect_timeout_ms"])
        #: 제한 시간 초과·경보 확인으로 순찰에 돌아간 뒤 다시 의심하지 않는 시간과 끝 시각.
        self._cooldown_ms = int(fsm["fall_resuspect_cooldown_ms"])
        self._cooldown_until: int | None = None
        self._patrol_vlm_ms = int(config["vision"]["vlm"]["patrol_interval_ms"])

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
        return self._pending is not None

    def suspect(self, source: str, now_ms: int, *, yes_asked: int | None = None) -> None:
        """쓰러짐 의심에 든다 — 순찰·구역 점검이면 `FALL_SUSPECTED`, 추종 중이면 그 자리에서.

        `yes_asked` 는 의심에 들게 한 판독을 건 시각이다 — 확정에 세지 않고 다음 «예» 의
        간격만 잰다. 재의심 쿨다운 안이면 들지 않는다.
        """
        if self._since is not None or not self._mission.enables("fallen"):
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
        self._vlm_yes = None
        self._confirmed = False
        # 5초 상실은 의심에 든 때부터 센다 — 판독으로만 든 의심에는 검출 시각이 없다.
        self._behavior.note_target(now_ms)
        self._escalation.note_fall_suspect(True, now_ms)
        LOG.warning("fall_suspected", source=source)

    def _confirm(self, frame: Any, now_ms: int) -> None:
        """«예» 가 모이면 기록을 남기고 `PERSON_DOWN`(L3, 전이 없음)을 낸다 (ADR-42)."""
        if self._confirmed or self._since is None or self._yes < self._need:
            return
        self._confirmed = True
        # ⚠️ **전이보다 먼저 남긴다** — 대시보드 사건이 기록을 가리키게.
        self._record(
            "person_fallen",
            frame,
            {
                "fallen": True,
                "vlm_yes": self._yes,
                "suspect_ms": now_ms - self._since,
                "raw": self._vlm_yes,
            },
        )
        self._apply(Event.PERSON_DOWN, now_ms)

    def watch(self, now_ms: int) -> None:
        """의심을 끝낼 때를 본다. 틱마다 — 새 프레임이 없어도 — 부른다.

        `ALERT`·`TRACK` 을 떠났으면 쿨다운 없이 끝내고, 확정하지 못한 채
        `fsm.fall_suspect_timeout_ms` 가 지나면 순찰로 돌려보낸다.
        """
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
        self._vlm_yes = None
        self._confirmed = False
        self._escalation.note_fall_suspect(False, now_ms)

    def take_reading(self, now_ms: int) -> None:
        """끝난 쓰러짐 판독을 줍는다. «예» 면 의심에 들거나 의심 뒤의 것이면 확정을 채운다.

        의심 중에 건 판독이 의심이 끝난 뒤에 오면 버리고, 앞서 센 «예» 와
        `fsm.fall_confirm_gap_ms` 안에 건 «예» 는 세지 않는다 (ADR-42).
        """
        if self._pending is None or self._vlm.busy:
            return
        (asked_ms, asked, during), self._pending = self._pending, None
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
            self._vlm_yes = next(a.raw for a in reading.answers if a.key == "person_down")
            self._confirm(asked, now_ms)

    def ask(self, result: VisionResult, now_ms: int) -> None:
        """`person_down` 하나만 묻는 판독을 건다 (ADR-35) — 순찰 중에는
        `vision.vlm.patrol_interval_ms` 마다, 확정 전 의심 중에는 판독이 끝날 때마다.

        쓰러짐·구역 판독 어느 쪽이든 돌고 있으면 걸지 않는다 — 워커의 결과 슬롯은 하나다.
        """
        if not self._mission.enables("fallen"):
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
