"""SIM 전용 사건·사진·음성 예시. 실물 런타임에서는 가져오지 않는다."""

import io
import time

from fastapi import APIRouter, Request
from PIL import Image, ImageDraw

from host.dashboard.state import DashboardState

ENTRIES = ("SIM-PPE-01", "SIM-FALL-02")


def snapshot(entry: str) -> bytes | None:
    if entry not in ENTRIES:
        return None
    image = Image.new("RGB", (640, 360), "#edf2eb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((220, 65, 355, 315), outline="#d97706", width=4)
    draw.text((25, 25), "SIMULATION / SYNTHETIC EVIDENCE", fill="#933314")
    draw.text((25, 45), entry + " - NOT A REAL PHOTO", fill="#933314")
    draw.ellipse((260, 80, 310, 130), fill="#52644a")
    draw.rectangle((245, 135, 325, 235), fill="#52644a")
    draw.line((260, 235, 245, 300), fill="#52644a", width=12)
    draw.line((310, 235, 330, 300), fill="#52644a", width=12)
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def record_examples(state: DashboardState) -> None:
    for entry, event, judgement in (
        (
            ENTRIES[0],
            "PPE_VIOLATION",
            {"state": "VIOLATION", "reason": "예시: 안전모 미착용", "track_id": 1, "zone": "A"},
        ),
        (
            ENTRIES[1],
            "person_fallen",
            {"aspect": 0.4, "still_ms": 3000, "confirm_ms": 2500, "zone": "B"},
        ),
    ):
        state.record_event(
            {
                "event": event,
                "ts_ms": int(time.time() * 1000),
                "state": "OBSERVE",
                "escalation": "L1",
                "mode": "factory",
                "simulated": True,
                "entry": entry,
                "snapshot": "snapshot.jpg",
                "telemetry": {"device_id": "SIM-route"},
                "tracks": [{"track_id": 1, "score": 0.9, "box": [220, 65, 355, 315]}],
                "detections": [],
                "judgement": judgement,
            }
        )


def voice_routes() -> APIRouter:
    app = APIRouter()
    events = [
        {"ts": "예시", "role": "admin", "text": "[예시] 안전모를 착용해 주세요."},
        {"ts": "예시", "role": "robot", "text": "[예시] 쓰러진 사람이 있는지 확인해 주세요."},
    ]
    mode = "standby"

    @app.get("/api/demo-voice/status")
    async def status():
        return {
            "robot": "SIM-route · 예시",
            "mode": mode,
            "activity": "예시 음성 · 실제 재생 없음",
            "say_queue": 0,
            "events": list(events),
        }

    @app.get("/api/demo-voice/transcript")
    async def transcript():
        return list(events)

    @app.get("/api/demo-voice/phrases")
    async def phrases():
        return [
            {
                "category": "예시 경고",
                "count": 2,
                "lines": [{"text": event["text"]} for event in events[:2]],
            }
        ]

    @app.get("/api/demo-voice/scenarios")
    async def scenarios():
        return [{"name": "sim-warning", "desc": "예시 보호구 경고 · 실제 방송 없음"}]

    @app.get("/api/demo-voice/report")
    async def report():
        return {
            "date": "예시",
            "total": len(events),
            "by_role": {"admin": 1, "robot": 1},
            "robot_events": {"예시 사건": 2},
            "scenario_runs": {},
            "warnings": [],
            "emergencies": [],
            "scenario_failures": [],
        }

    @app.post("/api/demo-voice/mode")
    async def change_mode():
        nonlocal mode
        mode = "active" if mode == "standby" else "standby"
        return {"ok": True, "mode": mode, "simulated": True}

    @app.post("/api/demo-voice/scenario")
    async def scenario(request: Request):
        body = await request.json()
        if body.get("name") != "sim-warning":
            return {"ok": False, "detail": "예시 시나리오 없음"}
        events.append(
            {
                "ts": "예시",
                "role": "admin",
                "text": "[예시 실행] 안전모를 착용해 주세요. 실제 방송 없음.",
            }
        )
        return {"ok": True, "simulated": True}

    return app
