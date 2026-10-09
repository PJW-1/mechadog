"""외부 3D 보기 설정은 개체 서버 상태를 통해 전달한다."""

from contextlib import contextmanager

from fastapi.testclient import TestClient

from host.dashboard.server import create_app
from host.dashboard.state import DashboardState


def test_scene_url_defaults_to_empty_and_is_exposed_without_fetching():
    state = DashboardState("test", stale_after_ms=3000)
    with TestClient(create_app(state)) as client:
        assert client.get("/health").json()["dashboard"] == {"scene3d_url": ""}
    with TestClient(create_app(state, scene3d_url="http://127.0.0.1:8791/")) as client:
        assert client.get("/health").json()["dashboard"] == {
            "scene3d_url": "http://127.0.0.1:8791/"
        }


def test_runtime_server_forwards_scene_and_simulation_capability(monkeypatch):
    from host.dashboard import server

    captured = {}

    def app_factory(_state, **options):
        captured.update(options)
        return "app"

    @contextmanager
    def serving(_app, _port):
        yield "server"

    monkeypatch.setattr(server, "create_app", app_factory)
    monkeypatch.setattr(server, "serving", serving)
    state = DashboardState("test", stale_after_ms=3000)
    with server.running_server(
        state, 8799, scene3d_url="http://127.0.0.1:8791/", simulated=False
    ) as running:
        assert running == "server"
    assert captured["scene3d_url"] == "http://127.0.0.1:8791/"
    assert captured["simulated"] is False
