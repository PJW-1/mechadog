"""웹 목업의 실제 API와 스캔 루프로 점 힌트 확정을 검증한다."""

import math
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient
from test_lidar_patrol import build, open_room

from tools.ops import route_mock_dashboard as mock
from tools.ops.patrol_mock import physical_hit_mask


def test_physical_mask_keeps_loc_walls_and_removes_confirmed_free_conflicts():
    nav, loc = open_room(), open_room()
    loc.cells[20, 20] = 3.0
    nav.cells[30, 30] = 0.0
    loc.cells[30, 30] = 3.0
    nav.cells[40, 40] = 3.0
    hits = physical_hit_mask(nav, loc, 1.0, -1.0)
    assert not hits[20, 20]
    assert hits[30, 30]
    assert not hits[40, 40]
    assert hits[0, 0]


def test_mock_point_hint_verifies_through_api_and_scan_loop(cfg, tmp_path, monkeypatch):
    nav, loc = open_room(), open_room()
    # 항법 지도에만 있는 점유 띠를 스캔하면 측위 지도의 벽과 정합할 수 없다.
    nav.cells[10:90, 65:70] = 3.0
    controller = build(grid=nav, loc_grid=loc, reloc_interval_ms=200, verify_interval_ms=500)
    home = (1.5, 1.2, math.degrees(0.3))
    monkeypatch.setattr(mock, "prepare", lambda *_args: (tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(mock, "load_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(mock, "PlanningService", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mock, "build_controller", lambda *_args: controller)
    monkeypatch.setattr(mock.ZoneMap, "load", lambda *_args: None)
    client = None
    observed = []
    real_sleep = time.sleep

    @contextmanager
    def serving(app, _port):
        nonlocal client
        with TestClient(app) as client:
            response = client.post("/api/command/locate", json={"x": home[0], "y": home[1]})
            assert response.json()["accepted"] is True
            assert client.get("/api/nav").json()["verified"] is False
            yield

    def tick_sleep(_seconds):
        observed.append(client.get("/api/nav").json())
        if observed[-1]["verified"] or len(observed) >= 150:
            raise KeyboardInterrupt
        real_sleep(0.02)  # 실제 정합 워커에 CPU 시간을 준다.

    monkeypatch.setattr(mock, "serving", serving)
    monkeypatch.setattr(mock.time, "sleep", tick_sleep)
    assert (
        mock.main(
            [
                "--maps",
                str(tmp_path),
                "--output",
                str(tmp_path),
                "--start",
                ",".join(map(str, home)),
                "--port",
                "8030",
            ]
        )
        == 0
    )
    assert observed[-1]["verified"] is True
    assert observed[-1]["stale"] is False
    assert observed[-1]["point_hint"] is None
    assert math.dist(observed[-1]["pose"][:2], home[:2]) < 0.15
