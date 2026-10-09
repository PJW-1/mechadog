"""사건 기록 — 블랙박스·관제 사건 목록·이력 DB 에 한 사건을 남기는 곳 (ADR-46).

장면 사건(사진 + 판단 근거)과 길 찾기 사건, 단계·전이 사건, 순찰 한 판의 열고 닫기와
방문 구역을 맡는다. ⚠️ **어느 저장소가 실패해도 기록만 하고 제어는 계속한다** — 기록이
10Hz 제어를 멈추면 로깅이 안전보다 앞서는 꼴이 된다. 모든 메서드는 운용 루프 스레드에서
부른다 (`Runtime.tick`·`_apply`·`release`).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from host.behavior.escalation import Escalation
from host.behavior.fsm import Behavior, Event
from host.behavior.mission import Mission
from host.behavior.speaker import Speaker
from host.behavior.zone_policy import ZonePpePolicy
from host.common.blackbox import BlackboxEntry, EventBlackbox
from host.common.history import (
    HistoryStore,
    IncidentRow,
    incident_from_entry,
    incident_from_feed,
)
from host.common.logging_setup import EdgeTrigger, event_logger
from host.dashboard.state import DashboardState
from host.telemetry.session_recorder import SessionRecorder
from host.vision.worker import VisionResult, VisionSource

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")

#: 관제 사건 목록에 올리는 FSM 전이 — 인증과 안전 래치 해제만. ⚠️ 순찰·추적 전이는
#: 싣지 않는다. `ALERT ⇄ TRACK` 은 초당 몇 번씩 왕복해 목록을 덮는다.
FEED_TRANSITIONS: dict[Event, str] = {
    Event.AUTH_REQUIRED: "auth_required",
    Event.AUTH_OK: "auth_granted",
    Event.AUTH_FAILED: "auth_failed",
    Event.RESET_CONFIRMED: "failsafe_cleared",
}
#: 순찰 한 판을 닫는 상태와 그 결과 (ADR-46 결정 5). 이 셋을 떠나면 판이 열린다.
RUN_ENDS: dict[str, str] = {"IDLE": "stopped", "MANUAL": "manual", "FAILSAFE": "failsafe"}
#: 길 찾기 사건에 붙일 원래 프레임을 이만큼만 들고 있는다 (오래된 것부터 버린다).
NAVIGATION_FRAMES_MAX = 32


class IncidentLog:
    """사건 하나를 블랙박스·관제 화면·이력 DB 에 남긴다. 셋 중 무엇이 없어도 된다."""

    def __init__(
        self,
        *,
        device_id: str,
        session_id: str,
        config_sha256: str,
        clock: Callable[[], int],
        behavior: Behavior,
        escalation: Escalation,
        mission: Mission,
        speaker: Speaker,
        zone_ppe: ZonePpePolicy,
        zone_ids: frozenset[str],
        zone: Callable[[], str | None],
        telemetry: Callable[[], Mapping[str, Any]],
        vision: VisionSource | None,
        recorder: SessionRecorder | None,
        dashboard: DashboardState | None,
        history: HistoryStore | None,
        blackbox: EventBlackbox | None,
        event_publisher: Callable[[BlackboxEntry], None] | None,
    ) -> None:
        self._device_id = device_id
        self._session_id = session_id
        self._config_sha256 = config_sha256
        self._clock = clock
        self._behavior = behavior
        self._escalation = escalation
        self._mission = mission
        self._speaker = speaker
        self._zone_ppe = zone_ppe
        self._zone_ids = zone_ids
        #: 지금 서 있는 점검 구역 (`ZoneInspector.zone`). 점검기가 이 기록기를 쓰므로 호출 때 찾는다.
        self._zone = zone
        #: 마지막 텔레메트리 요약. 런타임이 수신마다 새 dict 로 바꾸므로 호출 때 찾는다.
        self._telemetry = telemetry
        self._vision = vision
        self._recorder = recorder
        self._dashboard = dashboard
        # 사건·순찰 이력 색인 (ADR-46). 쓰기가 실패해도 기록만 하고 제어는 멈추지 않는다.
        self._history = history
        # 저장소와 대시보드 송신자를 주입한다. 정식 CLI는 저장소를 항상 연결하고,
        # FastAPI/WebSocket 서버가 생기면 같은 항목을 publisher로 받는다.
        # 둘을 분리해야 디스크 기록 성공과 브라우저 연결 여부가 서로 발목을 잡지 않는다.
        self.blackbox = blackbox
        self._event_publisher = event_publisher
        # 열려 있는 순찰 한 판과, 그 판에 마지막으로 남긴 도착 구역.
        self._mission_run: str | None = None
        self._visited_zone: str | None = None
        # 대시보드 «순찰 정지» 가 수동을 거치는 동안 켠다 — 그 `MANUAL` 진입은 `stopped` 로 닫는다.
        self._stopping_patrol = False
        #: 막힘 사건에 붙일 **막힌 그 순간**의 프레임 (막힘 id·목표 → 결과).
        self.navigation_frames: dict[int | str, VisionResult] = {}
        self._edge = EdgeTrigger()

    @property
    def mission_run(self) -> str | None:
        """열려 있는 순찰 한 판의 id. 없으면 `None`."""
        return self._mission_run

    def keep_navigation_frame(self, key: int | str, frame: VisionResult | None) -> None:
        """막힘 id·목표마다 처음 본 프레임을 하나 들고 있는다 (오래된 것부터 버린다)."""
        if key not in self.navigation_frames and frame is not None:
            self.navigation_frames[key] = frame
            if len(self.navigation_frames) > NAVIGATION_FRAMES_MAX:
                del self.navigation_frames[next(iter(self.navigation_frames))]

    def record_navigation_event(self, event: dict[str, Any], now_ms: int) -> None:
        kind = str(event["event"])
        judgement = dict(event["judgement"])
        result = self._vision.latest() if self._vision is not None else None
        key = judgement.get("blockage_id") or judgement.get("target") or judgement.get("zone")
        if isinstance(key, (int, str)):
            result = self.navigation_frames.get(key, result)
        judgement["camera_available"] = result is not None and bool(result.jpeg)
        judgement["camera_completed_ms"] = None if result is None else result.completed_ms
        judgement["camera_age_ms"] = (
            None if result is None else max(0, now_ms - result.completed_ms)
        )
        judgement["sentence"] = self._speaker.announce(kind, judgement)
        # ⚠️ 판정의 `zone` 은 설정 구역일 때만 사건 구역이다 — `전체`·`GOAL`·`지점 3` 을 그대로
        # 쓰면 `zones` 표에 자리표 구역이 생긴다 (DATA_MODEL 3.4). 두 경로가 같은 값을 쓴다.
        judged = judgement.get("zone")
        zone_id = judged if isinstance(judged, str) and judged in self._zone_ids else self._zone()
        # ⚠️ **블랙박스가 없어도 이력에는 남긴다** (ADR-46 결정 4) — 사진 없는 사건 행이 된다.
        if self.blackbox is None:
            self.remember(
                lambda: incident_from_feed(
                    {
                        "event": kind,
                        "ts_ms": now_ms,
                        "state": self._behavior.state,
                        "escalation": self._escalation.level.value,
                        "mode": self._mission.mode,
                        **judgement,
                    },
                    robot_id=self._device_id,
                    mission_id=self._mission_run,
                    zone_id=zone_id,
                )
            )
            return
        try:
            entry = self.blackbox.record(
                kind,
                jpeg=None if result is None else result.jpeg,
                tracks=() if result is None else result.tracks,
                detections=() if result is None else result.detections,
                judgement=judgement,
                telemetry=self._telemetry(),
                state=self._behavior.state,
                escalation=self._escalation.level.value,
                mode=self._mission.mode,
                now_ms=now_ms,
            )
        except Exception as exc:  # noqa: BLE001 — 사건 기록 실패가 10Hz 제어 루프를 멈추면 안 된다
            LOG.error("navigation_event_record_failed", error=f"{type(exc).__name__}: {exc}")
            return
        self.remember(
            lambda: replace(
                incident_from_entry(
                    entry,
                    robot_id=self._device_id,
                    mission_id=self._mission_run,
                    zone_id=zone_id,
                ),
                zone_id=zone_id,
            )
        )
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(entry)
        except Exception as exc:  # noqa: BLE001 — 관제 발행 실패가 10Hz 제어 루프를 멈추면 안 된다
            LOG.error("navigation_event_record_failed", error=f"{type(exc).__name__}: {exc}")

    def record_scene(
        self, event_type: str, result: VisionResult, judgement: dict[str, Any] | None = None
    ) -> None:
        """사진과 **그릴 수 없는 판단 근거**를 한자리에 남긴다.

        ⚠️ **검출 박스와 달리 이것들은 그림이 없다.** 쓰러짐 판정은 숫자이고
        VLM 판독은 문장이라, 사진 옆에 적어 두지 않으면 나중에 *"왜 그렇게
        판정했나"* 를 되짚을 방법이 없다.

        ⚠️ **문장 생성·방송은 기록이 없어도 나간다.** 관제가 그 순간 들어야
        할 경고이지 블랙박스 파일이 아니므로, 블랙박스가 없는 구성(`blackbox=None`)
        에서도 방송만은 막지 않는다.
        """
        now_ms = self._clock()
        judgement = dict(judgement or {})
        judgement.setdefault("zone", self._zone_ppe.current_zone(now_ms))
        judgement.setdefault(
            "ppe_required", list(self._zone_ppe.requirements_for(judgement["zone"]))
        )
        sentence: str | None = None
        if (
            event_type == "PPE_SETTLED"
            and judgement.get("rechecked")
            and judgement.get("reason") == "착용 확인"
        ):
            if self._dashboard is not None and (
                self.blackbox is None or self._event_publisher is None
            ):
                self._dashboard.record_event(
                    {
                        "event": event_type,
                        "ts_ms": now_ms,
                        "state": self._behavior.state,
                        "escalation": self._escalation.level.value,
                        "mode": self._mission.mode,
                        "judgement": judgement,
                    }
                )
            self._speaker.play("ppe_settled")
        elif event_type == "PPE_SETTLED" and (judgement or {}).get("reason") == "경고 횟수 한도":
            self._speaker.play("ppe_unresolved")
        cross_result = judgement is not None and "rule_yes" in judgement
        if (
            cross_result
            and self._dashboard is not None
            and (self.blackbox is None or self._event_publisher is None)
        ):
            self._dashboard.record_event(
                {
                    "event": event_type,
                    "ts_ms": self._clock(),
                    "state": self._behavior.state,
                    "escalation": self._escalation.level.value,
                    "mode": self._mission.mode,
                    "judgement": judgement,
                }
            )
        if judgement is not None and "rule_yes" in judgement and self._recorder is not None:
            self._recorder.record(
                "fall_cross_result", at_ms=self._clock(), frame_seq=result.frame_seq, **judgement
            )
        # 경비 모드의 쓰러짐은 기록만 남긴다 — 경보도 확인할 것도 없는 사건이라
        # 방송·자막 문장을 붙이지 않는다.
        if not (judgement or {}).get("test_mode") and (
            event_type != "person_fallen" or self._mission.enables("fallen")
        ):
            sentence = self._speaker.announce(event_type, judgement)
        if self.blackbox is None:
            return
        recorded_judgement = judgement
        if sentence is not None:
            recorded_judgement = {**(judgement or {}), "sentence": sentence}
        try:
            entry = self.blackbox.record(
                event_type,
                judgement=recorded_judgement,
                jpeg=result.jpeg,
                tracks=result.tracks,
                detections=result.detections,
                telemetry=self._telemetry(),
                state=self._behavior.state,
                escalation=self._escalation.level.value,
                mode=self._mission.mode,
                now_ms=result.completed_ms,
                **self._event_trace(result),
            )
        except Exception as exc:  # noqa: BLE001 — 기록 실패가 10Hz 제어를 죽이면 안 된다
            LOG.error("blackbox_record_failed", error=f"{type(exc).__name__}: {exc}")
            return
        self.remember(
            lambda: incident_from_entry(
                entry,
                robot_id=self._device_id,
                mission_id=self._mission_run,
                zone_id=self._zone(),
            )
        )
        if self._event_publisher is None:
            return
        try:
            self._event_publisher(entry)
        except Exception as exc:  # noqa: BLE001 — 브라우저 단절은 제어 실패가 아니다
            LOG.error("dashboard_event_publish_failed", error=f"{type(exc).__name__}: {exc}")

    def _event_trace(self, result: VisionResult) -> dict[str, Any]:
        """사건이 «어느 세션·장치·프레임·모델·설정으로, 얼마 걸려» 판단됐는지.

        지연은 같은 PC 시계로 잰 값만 싣는다 — 시계가 어긋나 음수가 나오면 뺀다.
        """
        latency: dict[str, Any] = {"inference_ms": round(result.inference_ms, 2)}
        for key, end_ms in (
            ("frame_to_result_ms", result.completed_ms),
            ("frame_to_decision_ms", self._clock()),
        ):
            if end_ms >= result.frame_received_ms:
                latency[key] = end_ms - result.frame_received_ms
        models = getattr(self._vision, "models", None)
        return {
            "session_id": self._session_id,
            "device_id": self._device_id,
            "frame_id": result.frame_seq,
            "config_sha256": self._config_sha256,
            "models": models() if callable(models) else None,
            "latency": latency,
        }

    def track_run(self, after: str, trigger: str | None, now_ms: int) -> None:
        """순찰 한 판을 열고 닫는다 (ADR-46 결정 5).

        대기·수동·페일세이프(`RUN_ENDS`)를 떠나면 열고, 그중 하나로 들어가면 닫는다.
        닫게 한 트리거가 `stop_reason` 이다. 대시보드 «순찰 정지» 가 거치는 `MANUAL` 은
        `stopped`·`patrol_stop` 으로 닫는다(`stopping_patrol`).
        """
        if self._history is None:
            return
        result = RUN_ENDS.get(after)
        if result is None:
            if self._mission_run is None:
                self._mission_run = self._history.open_run(
                    self._device_id, mode=self._mission.mode, started_at=now_ms
                )
            return
        if self._mission_run is not None:
            if result == "manual" and self._stopping_patrol:
                result, trigger = "stopped", "patrol_stop"
            self._history.close_run(
                self._mission_run, ended_at=now_ms, result=result, stop_reason=trigger
            )
            self._mission_run = None

    def close_run_on_shutdown(self) -> None:
        """런타임이 멈출 때 열린 판을 `shutdown` 으로 닫는다."""
        if self._history is not None and self._mission_run is not None:
            self._history.close_run(
                self._mission_run,
                ended_at=self._clock(),
                result="shutdown",
                stop_reason="runtime_stopped",
            )
            self._mission_run = None

    @contextlib.contextmanager
    def stopping_patrol(self) -> Iterator[None]:
        """대시보드 «순찰 정지» 의 `MANUAL_ON` 을 수동 조종과 구별한다 (ADR-46 결정 5).

        전이표에 자율 → `IDLE` 직행 사건이 없어 정지는 `MANUAL` 을 거친다. 이 안에서
        `MANUAL` 로 닫힌 판은 `manual` 이 아니라 `stopped`(`stop_reason` `patrol_stop`)로 남는다.
        """
        self._stopping_patrol = True
        try:
            yield
        finally:
            self._stopping_patrol = False

    def note_zone_visit(self) -> None:
        """구역에 새로 도착했으면 열린 판의 방문 구역에 더한다 (처음 도착한 순서).

        ⚠️ **판이 닫혀도 마지막 구역을 잊지 않는다.** 구역 점검은 같은 구역에 서 있는 동안
        다시 도착하지 않으므로, 그 자리에서 새 판을 시작하면 떠났다가 와야 방문이다.
        """
        zone = self._zone()
        if zone == self._visited_zone:
            return
        self._visited_zone = zone
        if zone is not None and self._history is not None and self._mission_run is not None:
            self._history.visit_zone(self._mission_run, zone)

    def remember(self, build: Callable[[], IncidentRow]) -> None:
        """사건 하나를 이력 DB 에 남긴다 (ADR-46).

        ⚠️ **실패는 기록만 한다.** DB 는 색인이고 원본은 블랙박스·JSONL 이다 — 색인이
        10Hz 제어를 멈추면 로깅이 안전보다 앞서는 꼴이 된다.
        """
        if self._history is None:
            return
        try:
            self._history.record_incident(build())
        except Exception as exc:  # noqa: BLE001 — 색인 실패가 10Hz 제어를 죽이면 안 된다
            LOG.error("history_record_failed", error=f"{type(exc).__name__}: {exc}")

    def announce_escalation(self, now_ms: int) -> None:
        """단계가 바뀐 **그 순간**을 사건으로 낸다 (FR-3.4).

        ⚠️ **상태가 아니라 단계에 건다.** `ALERT ⇄ TRACK` 왕복 체류가 0.2~0.6초로
        실측됐다. 상태 진입에 걸면 경고가 초당 몇 번씩 겹쳐 나간다.
        단계는 그 왕복에 영향받지 않으므로 엣지가 그대로 중복 억제가 된다.

        ⚠️ **블랙박스 사건에 얹지 않는다.** 그 사건은 사진을 저장할 때만 나가는데
        (`record_scene` 네 곳), 미인증 10초로 조용히 L2 가 되는 경우에는 그 넷 중
        아무 일도 일어나지 않는다. 얹어 두면 **경고가 늦거나 아예 안 나간다** —
        음성 쪽은 한참 뒤 엉뚱한 사건에 얹혀 온 값을 보고서야 알아챈다.

        ⚠️ **읽을 문장을 여기서 실어 보낸다.** 단계와 문구가 한곳에 있어야 단계를
        고칠 때 문구가 남지 않는다. 음성 쪽은 받은 문장을 읽기만 한다.
        """
        if self._dashboard is None and self._history is None:
            return
        level = self._escalation.level.value
        # ⚠️ **래치 여부도 엣지다.** 보호구 경고(L3·래치 아님) 중에 쓰러짐이 확정되면 단계는
        # L3 그대로라, 단계만 보면 경보 문장이 나가지 않는다 — 음성은 이 사건의 문장만 읽는다.
        if not self._edge.changed("escalation_level", (level, self._escalation.latched)):
            return
        self.publish_feed(
            {
                "event": "escalation_changed",
                "ts_ms": now_ms,
                "state": self._behavior.state,
                "escalation": level,
                "mode": self._mission.mode,
                "warning": self._escalation.presentation().warning,
                "reason": self._escalation.reason,
            }
        )
        reason = self._escalation.reason
        if level == "L1" and reason == "fall_suspected":
            self._speaker.play("fall_suspected")
        if self._escalation.presentation().warning and isinstance(reason, str):
            key = f"{reason.lower()}_warning"
            if reason == "PPE_VIOLATION":
                key = self._speaker.ppe_warning_key(key)
            self._speaker.play(key)

    def announce_transition(self, event: Event, previous: str, now_ms: int) -> None:
        """인증·안전 전이를 관제 사건으로 낸다. 전이 로그(jsonl)에만 있던 것들이다."""
        if self._dashboard is None and self._history is None:
            return
        after = self._behavior.state
        name = (
            "failsafe_entered"
            if after == "FAILSAFE" and previous != "FAILSAFE"
            else FEED_TRANSITIONS.get(event)
        )
        if name is not None:
            self.feed_event(name, now_ms, previous=previous, trigger=event.name)

    def feed_event(self, name: str, now_ms: int, **extra: Any) -> None:
        self.publish_feed(
            {
                "event": name,
                "ts_ms": now_ms,
                "state": self._behavior.state,
                "escalation": self._escalation.level.value,
                "mode": self._mission.mode,
                **extra,
            }
        )

    def publish_feed(self, payload: dict[str, Any]) -> None:
        """관제 사건 하나를 화면과 이력 DB 에 낸다. 둘 중 하나만 있어도 된다 (ADR-46 결정 4)."""
        if self._dashboard is not None:
            self._dashboard.record_event(payload)
        self.remember(
            lambda: incident_from_feed(
                payload,
                robot_id=self._device_id,
                mission_id=self._mission_run,
                zone_id=self._zone(),
            )
        )
