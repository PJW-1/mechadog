"""Y1: SIM capability와 실물 사건 계약을 통한 격리된 시연 자료."""

from fastapi.testclient import TestClient

from host.dashboard.server import DEFAULT_STATIC_DIR, create_app
from host.dashboard.state import DashboardState
from tools.ops import dashboard_demo


def test_simulation_contract_and_synthetic_evidence():
    state = DashboardState("SIM-route", stale_after_ms=3000)
    dashboard_demo.record_examples(state)
    app = create_app(
        state,
        static_dir=DEFAULT_STATIC_DIR,
        simulated=True,
        event_snapshot=dashboard_demo.snapshot,
        voice_path="/api/demo-voice",
        extra_routes=dashboard_demo.voice_routes(),
    )
    with TestClient(app) as client:
        assert client.get("/health").json()["capabilities"]["simulated"] is True
        events = client.get("/api/events").json()["events"]
        assert [event["event"] for event in events] == ["PPE_VIOLATION", "person_fallen"]
        for event in events:
            assert event["simulated"] is True
            photo = client.get(f"/events/{event['entry']}/snapshot.jpg")
            assert photo.status_code == 200
            assert photo.content.startswith(b"\xff\xd8")
        assert client.get("/events/unknown/snapshot.jpg").status_code == 404
        assert client.get("/api/demo-voice/status").json()["robot"].startswith("SIM")
        before = len(client.get("/api/demo-voice/transcript").json())
        assert (
            client.post("/api/demo-voice/scenario", json={"name": "sim-warning"}).json()[
                "simulated"
            ]
            is True
        )
        assert len(client.get("/api/demo-voice/transcript").json()) == before + 1
        assert client.post("/api/demo-voice/mode").json()["mode"] == "active"


def test_unspecified_connection_is_not_claimed_as_hardware():
    state = DashboardState("unknown", stale_after_ms=3000)
    with TestClient(create_app(state)) as client:
        assert client.get("/health").json()["capabilities"]["simulated"] is None
