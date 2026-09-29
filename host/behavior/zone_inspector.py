"""공장 모드의 구역 점검 — 앵커 도착·방향 맞추기·기준 비교·판독·종류별 결론 (FR-8 · ADR-41 · ADR-42).

반출·반입은 연속 2방문(`ChangeConfirmer`)에서, 넘어짐·통로 막힘은 같은 방문 안 판독 2회
«예» 에서 확정한다. 넘어짐·통로 막힘만 `ZONE_CHANGED`(L3) 이고 반출은 가벼운 경고,
반입은 기록만 한다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from host.behavior.change_detect import (
    PERSON_LABEL,
    BaselineStore,
    Change,
    ChangeConfirmer,
    ChangeKind,
    ZoneBaseline,
    classify_changes,
)
from host.behavior.fall_monitor import FallMonitor
from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.behavior.zones import Zone
from host.common.logging_setup import event_logger
from host.common.units import rad_to_deg, wrap_pi
from host.vision.vlm_worker import VlmWorker

if TYPE_CHECKING:
    from host.behavior.actions import TrackSequence
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")

#: 같은 방문에서 두 번 읽어 확정하는 판독 항목. `person_down` 은 여기 없다 — 그 «예»
#: 한 번은 쓰러짐 **의심**에 들 뿐이고(`ZoneInspector._take_reading`), `PERSON_DOWN` 확정은
#: 의심 뒤 판독 «예» `fsm.fall_confirm_vlm_yes`(2)회, 서로 `fsm.fall_confirm_gap_ms`(1000ms)
#: 이상 떨어진 프레임이어야 한다(`FallMonitor` · ADR-42).
ZONE_HAZARDS = ("fallen_object", "blocked_path")


class ZoneInspector:
    """구역 방문 하나를 점검하고 결론을 낸다.

    `ask_baseline_reset` 만 다른 스레드에서 부르고 나머지는 운용 루프 스레드에서 부른다.
    판독 워커는 쓰러짐 판독과 하나를 나눠 쓴다 — `FallMonitor.waiting` 이 참이면 걸지 않는다.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        behavior: Behavior,
        mission: Mission,
        vlm: VlmWorker,
        fall: FallMonitor,
        anchors: tuple[Zone, ...],
        apply: Callable[[Event, int], bool],
        record: Callable[[str, Any, dict[str, Any]], None],
    ) -> None:
        self._behavior = behavior
        self._mission = mission
        self._vlm = vlm
        self._fall = fall
        self._anchors = anchors
        self._apply = apply
        self._record = record
        #: 기준을 지우기로 한 구역 (`ask_baseline_reset`). 틱이 비운다.
        self._resets: set[str] = set()
        self._baselines = BaselineStore(config)
        self._confirmer = ChangeConfirmer(config)
        zones = config["zones"]
        self._ids = tuple(str(label) for label in zones["ids"])
        self._arrive_m = float(zones["arrival_radius_mm"]) / 1000.0
        self._align_tolerance_deg = float(zones["align_tolerance_deg"])
        self._align_turn_deg = float(zones["align_turn_deg"])
        self._align_timeout_ms = int(zones["align_timeout_ms"])
        self._pose_timeout_ms = int(config["localization"]["pose_timeout_ms"])
        #: 측위의 최신 위치 `((x m, y m, yaw rad), 받은 시각)`. `note_pose` 가 채운다.
        self._pose: tuple[tuple[float, float, float], int] | None = None
        #: 이번 방문 구역이 바라볼 방향(rad). `None` 이면 선 방향 그대로 본다.
        self._yaw: float | None = None
        #: 이번 방문에서 방향을 맞췄나. 맞추기 전에는 장면을 모으지 않는다.
        self._aligned = False
        self._turn = cast("TrackSequence", behavior.sequence_for("ZONE_INSPECT"))
        self._watch_classes = tuple(config["vision"]["coco"]["change_watch_classes"])
        self._zone: str | None = None
        # **방문 하나가 확정기의 사이클 하나다** (FR-8.4 · config `change_detect`). 한 방문에서
        # 사람 없는 프레임을 `visit_frames` 장 모아 과반에서 보인 변화만 관찰로 넣는다.
        change = config["change_detect"]
        self._visit_frames = int(change["visit_frames"])
        self._visit_max_ms = int(change["visit_max_ms"])
        #: VLM 판독으로 경보를 확정하나. 벤치 관문 전에는 끈다.
        self._vlm_hazards = bool(change["vlm_hazards"])
        self._visit_since_ms = 0
        #: 이번 방문에서 모은 사람 없는 프레임
        self._visit_seen: list[Any] = []
        #: 이번 방문에서 확정한 물건 변화와 그때의 기준. 떠날 때(`_leave`) 판독과 합친다.
        self._visit_found: tuple[Change, ...] = ()
        self._visit_baseline: ZoneBaseline | None = None
        #: 떠날 때 남길 결론 — `zone_clear`·`zone_unverified`, 이미 남겼으면 `None`
        self._visit_outcome: str | None = None
        #: 첫 판독이 «예» 라 한 항목. 두 번째 판독을 기다린다.
        self._suspects: tuple[str, ...] = ()
        #: 두 판독이 모두 «예» 라 한 항목 — 이번 방문에서 확정했다.
        self._hazards: tuple[str, ...] = ()
        #: 이번 방문에서 판독을 이미 시도했나. **방문당 한 번만 건다** — 거절당해도 다시
        #: 걸지 않는다. 걸지 못한 방문은 기다릴 것이 없어 곧바로 끝난다.
        self._asked = False
        #: 돌고 있는 판독을 건 `(구역, 프레임)`. 판독 결과에는 어느 구역의 것인지가 없어
        #: 건 자리에서 붙들어 두고 그 이름·그 사진으로 남긴다.
        self._pending: tuple[str, Any] | None = None
        #: 이번 방문에서 건 판독을 언제까지 기다리나. `None` 이면 기다릴 것이 없다.
        #: 상한은 판독 예산 그대로다 — 판독기도 예산을 넘기면 남은 질문을 버린다.
        self._wait_until: int | None = None
        self._wait_ms = int(config["vision"]["vlm"]["budget_ms"])
        #: 이번 방문의 점검이 끝났나. 끝났으면 판독·경보만 기다리고 다시 견주지 않는다.
        self._done = False
        #: 지금의 `ALERT` 가 구역 변화 확정으로 섰나. 섰으면 경보 확인이 순찰로 돌려보낸다.
        self._alarm_alert = False

    @property
    def waiting(self) -> bool:
        """구역 판독이 워커에 걸려 있나. 쓰러짐 판독은 이것이 참이면 걸지 않는다."""
        return self._pending is not None

    @property
    def alarm_alert(self) -> bool:
        """지금의 `ALERT` 가 구역 변화 확정으로 섰나."""
        return self._alarm_alert

    def forget_alarm(self) -> None:
        """확인하지 않은 구역 경보를 잊는다 — 순찰을 새로 시작할 때."""
        self._alarm_alert = False

    def note_pose(self, pose: tuple[float, float, float], now_ms: int) -> None:
        """측위의 최신 위치 `(x m, y m, yaw rad)` 를 받는다 (지도 좌표)."""
        self._pose = (pose, int(now_ms))

    def _fresh_pose(self, now_ms: int) -> tuple[float, float, float] | None:
        """`localization.pose_timeout_ms` 안의 위치. 낡았으면 모르는 위치다 (FR-6.6)."""
        if self._pose is None or now_ms - self._pose[1] > self._pose_timeout_ms:
            return None
        return self._pose[0]

    def _align(self, now_ms: int) -> bool:
        """앵커의 방향을 보고 있나. 아니면 제자리 회전을 지시한다 (ADR-40 과 같은 예외).

        지시는 `ZONE_INSPECT` 시퀀스가 명령 주기로 옮긴다. 위치를 모르면 돌지 않고 선다.
        """
        if self._yaw is None:
            return True
        pose = self._fresh_pose(now_ms)
        if pose is None:
            self._turn.note(0.0, 0.0, now_ms)
            return False
        error_deg = rad_to_deg(wrap_pi(self._yaw - pose[2]))
        if abs(error_deg) <= self._align_tolerance_deg:
            self._turn.note(0.0, 0.0, now_ms)
            return True
        # 요는 반시계가 양수이고 `MOVE angle` 도 양수가 좌회전이다(`_spin_angle`).
        self._turn.note(0.0, math.copysign(self._align_turn_deg, error_deg), now_ms)
        return False

    def inspect(self, result: VisionResult, now_ms: int) -> None:
        """구역 앵커에 닿으면 기준과 견준다 (FR-8). 새 프레임마다 부른다.

        객체 목록으로만 비교하고 픽셀은 보지 않는다(FR-8.2). 공장 모드의 `PATROL`·
        `ZONE_INSPECT` 에서만 돌지만 판독은 상태·모드와 무관하게 줍는다 (ADR-33).
        """
        # ⚠️ **판독은 상태·모드와 무관하게 줍는다.** 변화 확정으로 `ALERT` 에 갔거나
        # 상한을 넘겨 떠난 뒤에 온 결과도 건 구역의 것으로 남아야 한다.
        self._take_reading(result, now_ms)
        if not self._mission.enables("change_detect"):
            return
        state = self._behavior.state
        if state == "PATROL":
            pose = self._fresh_pose(now_ms)
            if pose is None:
                return  # 위치를 모르면 도착도 떠남도 판정하지 않는다
            anchor, distance = min(
                ((a, math.hypot(a.x - pose[0], a.y - pose[1])) for a in self._anchors),
                key=lambda pair: pair[1],
                default=(None, math.inf),
            )
            if distance > 2 * self._arrive_m:
                # ⚠️ **반경의 두 배를 벗어나야 떠난 것이다.** 비우지 않으면 다음 순회에 같은
                # 구역을 다시 점검하지 못하고, 반경에서 바로 비우면 가장자리에서 떨 때마다
                # 같은 구역을 거듭 점검한다.
                self._zone = None
                return
            if anchor is None or distance >= self._arrive_m or anchor.label == self._zone:
                return
            zone = anchor.label
            if self._apply(Event.ZONE_ARRIVED, now_ms):
                self._zone = zone
                self._yaw = anchor.yaw
                self._aligned = anchor.yaw is None  # 방향이 없으면 선 채로 본다
                self._asked = False
                self._wait_until = None
                self._done = False
                self._visit_since_ms = now_ms
                self._visit_seen = []
                self._visit_found = ()
                self._visit_baseline = None
                self._visit_outcome = None
                self._suspects = ()
                self._hazards = ()
                # ⚠️ **확정기의 누적은 지우지 않는다.** 방문 하나가 사이클 하나라, 도착할 때
                # 지우면 연속 2방문을 셀 수 없고 이미 확정한 반출이 바퀴마다 다시 울린다.
                LOG.info("zone_arrived", zone=zone)
            return
        if state != "ZONE_INSPECT" or self._zone is None:
            return
        if self._done:
            self._leave(result, now_ms)  # 점검은 끝났고 판독·경보를 기다리는 중이다
            return
        if not self._aligned:
            if not self._align(now_ms):
                # ⚠️ **영원히 돌지 않는다.** 못 맞춘 방문은 못 본 방문이다 — 기준도 뜨지 않는다.
                if now_ms - self._visit_since_ms >= self._align_timeout_ms:
                    LOG.warning("zone_align_timeout", zone=self._zone)
                    self._done = True
                    self._visit_outcome = "zone_unverified"
                    self._leave(result, now_ms)
                return
            self._aligned = True
            self._visit_since_ms = now_ms  # 방문 한도(`visit_max_ms`)는 방향을 맞춘 뒤부터 센다

        zone = self._zone
        width = int(getattr(result, "frame_width", 0) or 0)
        height = int(getattr(result, "frame_height", 0) or 0)
        if width <= 0 or height <= 0:
            return
        self._read_scene(zone, result, now_ms)
        # ⚠️ **사람이 보이는 프레임은 기준에도 비교에도 쓰지 않는다** (FR-8.3 → FR-3 ·
        # FR-11.1). 사람은 물체 변화가 아니라 게이트(`PERSON_FOUND`)가 맡는다. 그리고 **사람이
        # 물건을 가리면 그 물건이 빠진다.** 견주면 반출로 세어지고, 기준으로 뜨면 그 뒤
        # 순찰마다 «반입» 이 된다(기준은 없을 때만 뜨므로 누가 지우기 전까지 풀리지 않는다).
        if not any(detection.label == PERSON_LABEL for detection in result.detections):
            self._visit_seen.append(result)
        # ⚠️ **영원히 서 있지 않는다.** 사람이 비키기를 기다리는 것도 `visit_max_ms` 까지다.
        if (
            len(self._visit_seen) < self._visit_frames
            and now_ms - self._visit_since_ms < self._visit_max_ms
        ):
            return
        self._done = True
        frames = self._visit_seen
        if len(frames) < self._visit_frames:
            # 못 본 방문은 관찰이 아니다 — **세면** 한 번 보고 확정하는 꼴이 되고, **끊으면**
            # 사람이 오가는 구역의 반출은 영영 확정되지 않는다. 확정기에 넣지 않는다.
            # `zone_clear` 로 적지도 않는다 — 로그만 보고 그 구역이 확인된 줄 안다.
            self._visit_outcome = "zone_unverified"
            self._leave(result, now_ms)
            return
        # ⚠️ **기준 파일이 10Hz 제어를 죽이면 안 된다** — `Runtime._record_scene` 과 같다.
        # `serve` 는 수신만 감싸므로 여기서 던지면 프로세스가 끝나고, 재기동해도 이
        # 구역에 닿을 때마다 반복된다. 읽지 못한 기준을 지금 장면으로 덮어쓰지도
        # 않는다 — 그 사이의 변화가 조용히 기준이 된다. 사람이 보고 지우게 둔다.
        try:
            baseline = self._baselines.load(zone)
        except Exception as exc:  # noqa: BLE001 — 잘린 JSON 은 AttributeError 까지 낸다
            LOG.error("zone_baseline_unreadable", zone=zone, error=f"{type(exc).__name__}: {exc}")
            self._leave(result, now_ms)
            return
        if baseline is None:
            # FR-8.1 — 기준이 없으면 **이번 방문이 기준이다.** 기준 없이 견주면 처음 보는
            # 물건이 전부 반입으로 잡혀 첫 순찰이 경보로 뒤덮인다. 가장 많이 본 프레임을
            # 뜬다 — 깜빡여 물건을 놓친 프레임이 기준이 되면 그 물건이 바퀴마다 «반입» 이다.
            best = max(frames, key=lambda frame: len(frame.detections))
            try:
                self._visit_baseline = self._baselines.register(
                    zone,
                    best.detections,
                    frame_size=(width, height),
                    now_ms=now_ms,
                    jpeg=best.jpeg,
                )
            except Exception as exc:  # noqa: BLE001 — 위 `load` 와 같은 이유다
                LOG.error(
                    "zone_baseline_unwritable", zone=zone, error=f"{type(exc).__name__}: {exc}"
                )
            else:
                LOG.info("zone_baseline_registered", zone=zone, objects=len(best.detections))
                # 옛 기준으로 센 횟수가 새 기준의 확정을 앞당기면 안 된다.
                self._confirmer.forget(zone)
            self._leave(result, now_ms)
            return
        # ⚠️ **검출기는 깜빡인다.** 프레임 과반에서 보인 변화만 이번 방문의 관찰이다.
        seen: Counter[Change] = Counter(
            change
            for frame in frames
            for change in classify_changes(
                baseline,
                frame.detections,
                frame_size=(width, height),
                watch_classes=self._watch_classes,
            )
        )
        observed = [change for change, count in seen.items() if count * 2 > len(frames)]
        if observed:
            # 확정 전의 관찰이다 — 경보가 아니지만 연속이 어디까지 왔는지는 로그로 남긴다.
            LOG.info("zone_change_seen", zone=zone, changes=[c.as_dict() for c in observed])
        self._visit_found = self._confirmer.observe(zone, observed)
        self._visit_baseline = baseline
        self._visit_outcome = "zone_clear"
        self._leave(result, now_ms)

    def _read_scene(self, zone: str, result: VisionResult, now_ms: int) -> None:
        """구역에 선 동안 장면 판독을 방문당 한 번 건다 (ADR-35 호출 시점 ②).

        막지 않는다 — 결과는 다음 틱에 줍고 구역 종료만 미룬다(`_leave`). 걸지 못했으면
        `zone_reading_skipped` 로 사유를 남긴다.
        """
        if self._asked:
            return
        if self._fall.waiting:
            return  # 쓰러짐 판독이 끝나면 다음 틱에 건다 — 워커는 하나다(`FallMonitor.ask`)
        self._asked = True
        # ⚠️ **앞 판독을 줍기 전에는 걸지 않는다.** 걸면 슬롯에 남은 앞 결과가 새로 건
        # 구역의 것으로 읽힌다 — 스레드가 끝나는 순간과 거는 순간이 겹치면 그렇게 된다.
        if self._pending is None and self._vlm.submit(result.jpeg, now_ms=now_ms):
            self._pending = (zone, result)
            self._wait_until = now_ms + self._wait_ms
            LOG.info("zone_reading_requested", zone=zone)
            return
        LOG.info(
            "zone_reading_skipped",
            zone=zone,
            reason="busy" if self._vlm.available else "not_loaded",
        )

    def _take_reading(self, result: VisionResult, now_ms: int) -> None:
        """끝난 판독을 줍고 건 구역 이름과 건 프레임으로 남긴다.

        `person_down` «예» 는 쓰러짐 의심 진입 신호다(ADR-42). 넘어짐·통로 막힘은 이번
        방문에서 건 판독 두 번이 모두 «예» 인 항목만 확정한다(`vlm_hazards` · ADR-41).
        """
        if self._pending is None or self._vlm.busy:
            return
        (zone, asked), self._pending = self._pending, None
        # 이번 방문에서 건 판독이면 기다림도 여기서 끝난다.
        mine, self._wait_until = self._wait_until is not None, None
        reading = self._vlm.take()
        if reading is None:
            return  # 스레드가 죽었다 — 사유는 `vlm_worker_failed` 가 남겼다
        answers = {answer.key: answer.value for answer in reading.answers}
        raw = {answer.key: answer.raw for answer in reading.answers}
        LOG.info(
            "zone_reading", zone=zone, degraded=reading.degraded, reason=reading.reason, **answers
        )
        if mine and self._vlm_hazards:
            # 저하된 판독은 «빈 채널» 이다 — 위 `zone_reading` 이 사유를 남겼다.
            said = () if reading.degraded else tuple(k for k in ZONE_HAZARDS if reading.get(k))
            if self._suspects:
                self._hazards = tuple(k for k in self._suspects if k in said)
                self._suspects = ()
            elif said and self._behavior.state == "ZONE_INSPECT":
                # `inspect` 는 새 프레임에서만 부르므로 `result` 는 건 프레임보다 뒤다.
                if self._vlm.submit(result.jpeg, now_ms=now_ms):
                    self._suspects = said
                    self._pending = (zone, result)
                    self._wait_until = now_ms + self._wait_ms
                    LOG.info("zone_reading_requested", zone=zone, again=list(said))
                else:
                    reason = "busy" if self._vlm.available else "not_loaded"
                    LOG.info("zone_reading_skipped", zone=zone, reason=reason, again=list(said))
        self._record(
            "zone_reading",
            asked,
            {
                "zone": zone,
                "degraded": reading.degraded,
                "reason": reading.reason,
                "answers": answers,
                # ⚠️ **원문을 함께 남긴다.** 판독이 이상할 때 사람이 볼 것은 참/거짓이
                # 아니라 모델이 실제로 뱉은 글이다.
                "raw": raw,
                "latency_ms": {answer.key: answer.latency_ms for answer in reading.answers},
            },
        )
        if reading.get("person_down"):
            self._fall.suspect("zone_vlm", now_ms, yes_asked=now_ms)

    def _leave(self, result: VisionResult, now_ms: int) -> None:
        """방문을 끝낸다 — 결론은 여기 한 곳에서 종류별로 낸다 (ADR-41 · ADR-42).

        넘어짐·통로 막힘만 `zone_changed` → `ZONE_CHANGED`(L3), 반출은 `zone_notice`,
        반입은 로그만 남긴다. 물건 변화를 확정하지 않았고 판독이 남았으면 `budget_ms`
        까지 결론을 미룬다.
        """
        self._done = True
        if self._wait_until is not None and not self._visit_found:
            if now_ms < self._wait_until:
                return
            # 상한 초과는 기능 저하다. 결과가 늦게 오면 그때 건 구역 이름으로 남는다.
            LOG.warning("zone_reading_timeout", zone=self._zone, wait_ms=self._wait_ms)
            self._wait_until = None
        removed = [c.as_dict() for c in self._visit_found if c.kind is ChangeKind.REMOVED]
        added = [c.as_dict() for c in self._visit_found if c.kind is not ChangeKind.REMOVED]
        hazards = [{"kind": kind, "source": "vlm"} for kind in self._hazards]
        if hazards:
            changes = removed + added + hazards
            LOG.warning("zone_changed", zone=self._zone, changes=changes)
            baseline = self._visit_baseline
            # ⚠️ **전이보다 먼저 남긴다** — `Runtime._observe_fallen` 과 같다. 대시보드 사건
            # 피드는 이 기록을 받으므로, 없으면 화면에는 단계 변경만 보인다 (FR-8.3).
            self._record(
                "zone_changed",
                self._visit_seen[-1] if self._visit_seen else result,
                {
                    "zone": self._zone,
                    "grid": None if baseline is None else list(baseline.grid),
                    "changes": changes,
                    "baseline_ms": None if baseline is None else baseline.captured_ms,
                    "baseline_snapshot": None if baseline is None else baseline.snapshot,
                },
            )
            # 한 번만 남긴다 — 전이가 거절돼도 다음 틱에 같은 기록을 되풀이하지 않는다.
            self._visit_found = self._hazards = ()
            # 전이가 에스컬레이션을 L3 로 올린다 (`escalation` 표 · FR-8.4).
            self._alarm_alert = self._apply(Event.ZONE_CHANGED, now_ms)
            return
        if added:
            # Z3 — 반입은 기록만 한다. 관제 화면에 올리면 작업 중 흔한 물건 배치까지
            # 알림이 되어 «가벼운 경고» 의 뜻이 없어진다.
            LOG.info("zone_change_recorded", zone=self._zone, changes=added)
        if removed:
            # Z2 — 반출은 눈·문구·L3 없이 관제에 가벼운 경고만 남기고 순찰을 잇는다.
            LOG.warning("zone_notice", zone=self._zone, changes=removed)
            self._record(
                "zone_notice",
                self._visit_seen[-1] if self._visit_seen else result,
                {"zone": self._zone, "changes": removed},
            )
        self._visit_found = ()
        if self._visit_outcome is not None:
            LOG.info(self._visit_outcome, zone=self._zone, frames=len(self._visit_seen))
        self._asked = False
        self._apply(Event.ZONE_CLEAR, now_ms)

    def ask_baseline_reset(self, zone: str) -> tuple[bool, str]:
        """구역 기준 재등록을 예약한다. **다른 스레드에서 부른다** — 지우는 것은 다음 틱이다.

        경보(L3)는 풀지 않는다(ADR-26 과 같은 이유). `zones.ids` 에 있는 구역만 받는다 —
        파일 이름이 되는 값이다.
        """
        if zone not in self._ids:
            return False, f"설정에 없는 구역이다: {zone!r}"
        self._resets.add(zone)
        return (
            True,
            f"구역 {zone} 의 기준을 다음 틱에 지운다 — 다음에 볼 때(점검 중이면 이번 장면) 새로 뜬다",
        )

    def reset_baselines(self) -> None:
        """예약된 기준을 지우고 그 구역의 확정기 누적도 지운다. 운용 루프에서만 부른다."""
        # 서버 스레드가 `add` 하고 여기서만 `pop` 한다 — 둘 다 원자적이고 소비자는 하나다.
        while self._resets:
            zone = self._resets.pop()
            # ⚠️ **기준 파일이 10Hz 제어를 죽이면 안 된다** — `inspect` 와 같다.
            try:
                existed = self._baselines.clear(zone)
            except Exception as exc:  # noqa: BLE001 — Windows 는 쥔 파일을 지우지 못한다
                LOG.error(
                    "zone_baseline_reset_failed", zone=zone, error=f"{type(exc).__name__}: {exc}"
                )
                continue
            # 옛 기준으로 센 횟수가 새 기준의 확정을 앞당기면 안 된다.
            self._confirmer.forget(zone)
            LOG.info("zone_baseline_reset", zone=zone, existed=existed)
