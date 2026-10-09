"""관제 서버(`create_app`)에 넘길 런타임 연결 — 명령·영상·사건 그림·정책·지도.

한 대짜리 런타임(`host.runtime_cli`)과 여러 대(`host.fleet`)가 같은 연결을 쓴다. 여기서는
아무것도 만들지 않고 **이미 만든 런타임·비전·블랙박스를 화면 쪽 인터페이스로 묶기만** 한다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.common.blackbox import BlackboxEntry, EventBlackbox
from host.dashboard.state import DashboardState
from host.vision.worker import VisionSource

if TYPE_CHECKING:
    from host.cloud import broadcast
    from host.runtime import Runtime


def _latest_jpeg(vision: VisionSource) -> Callable[[], bytes | None]:
    """대시보드 카메라 경로가 매번 호출하는 최신 프레임 공급자."""

    def grab() -> bytes | None:
        result = vision.latest()
        return result.jpeg if result is not None else None

    return grab


def dashboard_wiring(
    runtime: Runtime,
    config: Mapping[str, Any],
    *,
    vision: VisionSource | None,
    blackbox: EventBlackbox | None,
    broadcaster: broadcast.Broadcaster | None = None,
) -> dict[str, Any]:
    """관제 서버(`create_app`)에 넘길 명령·영상·사건 그림·정책 연결. 한 대·여러 대가 같이 쓴다."""
    from host.dashboard.commands import CommandService
    from host.dashboard.planning import PlanningService

    commands = CommandService(
        runtime.behavior,
        runtime.commander,
        runtime.send_immediate,
        emergency_stop=runtime.send_emergency_stop,
        request_reset=runtime.ask_reset,
        apply_event=runtime.apply_external,
        ask_patrol=runtime.ask_patrol,
        set_mode=runtime.set_mode,
        note_voice_auth=runtime.note_voice_auth,
        note_voice_listening=runtime.note_voice_listening,
        confirm_alarm=runtime.ask_alarm_confirm,
        locate_zone=runtime.ask_locate_zone,
        locate_point=runtime.ask_locate_point,
        goto_point=getattr(runtime, "ask_goto", None),
        start_route=getattr(runtime, "ask_route", None),
        stop_route=getattr(runtime, "ask_route_stop", None),
        stopping_patrol=runtime.stopping_patrol,
        pose=(
            float(config["posture"]["pitch_up_deg"]),
            int(config["posture"]["settle_ms"]),
            float(config["posture"].get("roll_offset_deg", 0.0)),
        ),
    )
    return {
        "commands": commands,
        "simulated": False,
        "scene3d_url": config.get("dashboard", {}).get("scene3d_url", ""),
        "camera": _latest_jpeg(vision) if vision is not None else None,
        # 박스와 그 박스를 계산한 JPEG 를 함께 보낸다.
        "vision": vision.latest if vision is not None else None,
        # 상태 칸의 로드된 모델·추론 지연 p50/p95 (`VisionWorker.status`).
        "vision_status": getattr(vision, "status", None),
        # 사건 전문에는 디렉터리 이름만 실으므로 그림은 여기서
        # 꺼낸다. 이름 검증은 저장 구조를 아는 블랙박스가 한다.
        "event_snapshot": None if blackbox is None else blackbox.snapshot_bytes,
        "policy": policy_view(config),
        # PC 스피커 방송 음량·무음 조절. 없으면(piper 없음 등) None —
        # 화면은 "방송 없음" 을 보여 준다.
        "broadcast": broadcaster,
        # 실제 집 지도·자기 위치 (LiDAR 측위 순찰일 때만).
        "map_view": _map_view(runtime),
        "nav_status": runtime.nav_status if _has_navigator(runtime) else None,
        "planning": PlanningService(dict(config), runtime.context.device_id),
        # 사건·순찰 이력 조회(`/api/history`). 런타임이 쓰는 저장소를 그대로 읽는다 (ADR-46).
        "history": runtime.history,
    }


def _has_navigator(runtime: Runtime) -> bool:
    return runtime.navigator is not None


def _map_view(runtime: Runtime) -> Callable[[], tuple[bytes, dict[str, Any]]] | None:
    """항법 지도·구역을 관제 웹에 그릴 함수. 처음 부를 때 한 번 그린다."""
    navigator = runtime.navigator
    if navigator is None:
        return None
    from host.dashboard.live_map import MapView, PoseFrame, render

    view = MapView(
        lambda: render(
            navigator.grid,
            zones=navigator.zones,
            zone_map=navigator.zone_map,
            frame=PoseFrame.load(navigator.maps_dir),
            occ_thresh=navigator.plan_params.occ_thresh,
            free_thresh=navigator.plan_params.free_thresh,
        )
    )
    return view.get


def _publish_event(dashboard: DashboardState) -> Callable[[BlackboxEntry], None]:
    """블랙박스 기록 하나를 관제 화면이 읽을 형태로 바꿔 넘긴다.

    ⚠️ **JPEG 바이트를 보내지 않는다.** 사건 채널은 상태 전문과 같은 JSON 소켓이고,
    프레임 하나가 수십 KB 다 — 거기 실으면 사건 하나가 텔레메트리를 밀어낸다.
    그림은 블랙박스가 디스크에 갖고 있으므로 **가리키는 이름만** 보낸다.

    ⚠️ **절대 경로를 브라우저에 보내지 않는다.** 화면에 쓸 일이 없고 PC 의 폴더
    구조를 드러낸다. 기록 디렉터리 이름이면 되돌아 찾을 수 있다.
    """

    def publish(entry: BlackboxEntry) -> None:
        dashboard.record_event(
            {
                "event": entry.event_type,
                "ts_ms": entry.ts_ms,
                "state": entry.state,
                "escalation": entry.escalation,
                "mode": entry.mode,
                # 비주 대상도 함께 남긴다 (FR-3.8.4) — 주 대상만 보내면 옆에 있던
                # 사람이 기록에서 사라진다.
                "tracks": entry.tracks,
                "detections": entry.detections,
                "telemetry": entry.telemetry,
                "entry": entry.meta_path.parent.name,
                "snapshot": entry.jpeg_path.name if entry.jpeg_path is not None else None,
                # 그릴 수 없는 판단 근거 — PPE 판정·쓰러짐 수치·VLM 판독 (B2).
                "judgement": entry.judgement,
                # 사건 추적 — 어느 세션·프레임·모델로, 얼마 걸려 판단했나.
                "event_id": entry.event_id,
                "session_id": entry.session_id,
                "frame_id": entry.frame_id,
                "config_sha256": entry.config_sha256,
                "models": entry.models,
                "latency": entry.latency,
            }
        )

    return publish


def policy_view(config: Mapping[str, Any]) -> dict[str, Any]:
    """설정 화면의 대응 단계 표가 읽는 값 (B7). **읽는 키는 판정 코드와 같다.**

    화면에 숫자를 따로 적어 두면 config 를 고쳐도 화면이 옛 값을 말한다.
    """
    esc, auth, vision = config["escalation"], config["auth"], config["vision"]
    return {
        "detect_window_ms": int(vision["detect_window_ms"]),
        "detect_hits_required": int(vision["detect_hits_required"]),
        "l1_to_l2_hold_s": int(esc["l1_to_l2_hold_s"]),
        "target_lost_timeout_s": int(config["fsm"]["target_lost_timeout_s"]),
        "auth_timeout_s": int(auth["timeout_s"]),
        "auth_max_attempts": int(auth["max_attempts"]),
        "auth_session_valid_s": int(auth["session_valid_s"]),
        "auth_verdict_grace_s": auth.get("verdict_grace_s"),
        "auth_require_both": bool(auth.get("require_both", False)),
        "l3_warning": (esc.get("sound") or {}).get("l3_warning"),
        "led": {key: value for key, value in esc["led"].items() if key != "l3_blink_hz"},
        # «위치 알려주기» 버튼 목록 — 순찰 구역 id (방 이름이 아니라 구역 기호).
        "patrol_zones": [str(zone) for zone in config["zones"]["ids"]],
    }
