"""사건 이력 조회 API 검증 (WBS 4.6.6 · ADR-46) — `/api/history/*`."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from host.common.history import HistoryStore, IncidentRow
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState

ROBOT = "mechdog-02"
REVIEW = {"reviewed": True}


@pytest.fixture
def store(tmp_path: Path):
    opened = HistoryStore(tmp_path / "history.sqlite3")
    opened.upsert_robot(ROBOT, serial_no=None)
    run = opened.open_run(ROBOT, mode="patrol", started_at=1000)
    for index, (event, zone, level) in enumerate(
        [("ppe_violation", "A", "warning"), ("fall", "B", "critical"), ("ppe_violation", "A", None)]
    ):
        opened.record_incident(
            IncidentRow(
                incident_id=f"{2000 + index}_{event}",
                robot_id=ROBOT,
                occurred_at=2000 + index,
                event_type=event,
                mission_id=run,
                zone_id=zone,
                escalation_level=level,
                blackbox_entry=f"{2000 + index}_{event}",
            )
        )
    yield opened
    opened.close()


def make_client(history):
    app = create_app(DashboardState(ROBOT, stale_after_ms=3000), history=history)
    return TestClient(app)


def call(http, method, path):
    return getattr(http, method)(path, **({"json": REVIEW} if method == "post" else {}))


@pytest.fixture
def client(store):
    with make_client(store) as http:
        yield http


def test_incidents_page_shape_and_order(client):
    body = client.get("/api/history/incidents").json()
    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (50, 0)
    assert [item["incident_id"] for item in body["items"]] == [
        "2002_ppe_violation",
        "2001_fall",
        "2000_ppe_violation",
    ]


def test_incidents_filters_and_paging(client):
    def ids(**params):
        body = client.get("/api/history/incidents", params=params).json()
        return body["total"], [item["incident_id"] for item in body["items"]]

    assert ids(zone="A") == (2, ["2002_ppe_violation", "2000_ppe_violation"])
    assert ids(event="fall") == (1, ["2001_fall"])
    assert ids(escalation="critical")[0] == 1
    assert ids(robot=ROBOT)[0] == 3
    assert ids(since=2001, until=2001) == (1, ["2001_fall"])
    assert ids(mission=f"{ROBOT}-1000")[0] == 3
    assert ids(reviewed="false")[0] == 3
    assert ids(reviewed="true")[0] == 0
    body = client.get("/api/history/incidents", params={"limit": 1, "offset": 1}).json()
    assert body["total"] == 3 and len(body["items"]) == 1
    assert (body["limit"], body["offset"]) == (1, 1)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"limit": "x"},
        {"offset": -1},
        {"since": -1},
        {"until": "x"},
        {"reviewed": "maybe"},
    ],
)
def test_incidents_bad_query_is_422(client, params):
    assert client.get("/api/history/incidents", params=params).status_code == 422


def test_incident_detail_and_404(client):
    assert client.get("/api/history/incidents/2001_fall").json()["zone_id"] == "B"
    assert client.get("/api/history/incidents/nope").status_code == 404


def test_runs_robots_zones(client):
    runs = client.get("/api/history/runs", params={"robot": ROBOT}).json()
    assert runs["total"] == 1 and runs["items"][0]["incident_count"] == 3
    assert (runs["limit"], runs["offset"]) == (50, 0)
    assert client.get("/api/history/runs", params={"robot": "x"}).json()["total"] == 0
    assert client.get("/api/history/runs", params={"limit": 0}).status_code == 422
    assert [r["robot_id"] for r in client.get("/api/history/robots").json()["items"]] == [ROBOT]
    assert {z["zone_id"] for z in client.get("/api/history/zones").json()["items"]} == {"A", "B"}


def test_review_round_trip(client):
    url = "/api/history/incidents/2001_fall/review"
    done = client.post(url, json={"reviewed": True, "resolution": "확인함"})
    assert done.status_code == 200
    assert done.json()["reviewed"] is True
    assert done.json()["resolution"] == "확인함"
    assert done.json()["reviewed_at"] is not None
    assert client.get("/api/history/incidents/2001_fall").json()["reviewed"] is True
    undone = client.post(url, json={"reviewed": False}).json()
    assert undone["reviewed"] is False and undone["resolution"] == ""
    assert undone["reviewed_at"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reviewed": "yes"},
        {"reviewed": 1},
        {"reviewed": True, "resolution": 5},
        {"reviewed": True, "resolution": "x" * 2001},
        [],
    ],
)
def test_review_bad_body_is_422(client, body):
    response = client.post("/api/history/incidents/2001_fall/review", json=body)
    assert response.status_code == 422


def test_review_unknown_is_404(client):
    response = client.post("/api/history/incidents/nope/review", json=REVIEW)
    assert response.status_code == 404


ROUTES = [
    ("get", "/api/history/incidents"),
    ("get", "/api/history/incidents/x"),
    ("get", "/api/history/runs"),
    ("get", "/api/history/robots"),
    ("get", "/api/history/zones"),
    ("post", "/api/history/incidents/x/review"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_disabled_history_is_503(method, path):
    with make_client(None) as http:
        response = call(http, method, path)
    assert response.status_code == 503
    assert response.json()["detail"] == "history_disabled"


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_store_error_is_503(store, method, path):
    store.close()  # 닫힌 연결은 sqlite3.ProgrammingError (sqlite3.Error)
    with make_client(store) as http:
        response = call(http, method, path)
    assert response.status_code == 503
    assert response.json()["detail"] == "history_unavailable"


def test_store_value_error_is_422():
    class Stub:
        def incidents(self, **_):
            raise ValueError("limit")

    with make_client(Stub()) as http:
        assert http.get("/api/history/incidents").status_code == 422


def test_foreign_origin_is_refused(client):
    foreign = {"origin": "https://other.example"}
    assert client.get("/api/history/incidents", headers=foreign).status_code == 403
    response = client.post("/api/history/incidents/2001_fall/review", json=REVIEW, headers=foreign)
    assert response.status_code == 403
    assert client.get("/api/history/incidents/2001_fall").json()["reviewed"] is False
    local = {"origin": "http://127.0.0.1"}
    assert client.get("/api/history/incidents", headers=local).status_code == 200
