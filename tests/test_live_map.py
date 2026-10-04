"""관제 웹 실제 지도 — 그림과 픽셀↔순찰 좌표 행렬이 서로 맞는가."""

from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from host.behavior.zones import ZoneStore
from host.dashboard.live_map import MapView, PoseFrame, render
from host.slam.occupancy import MapMeta, OccupancyGrid


def _grid() -> OccupancyGrid:
    grid = OccupancyGrid(
        MapMeta(resolution=0.05, origin_x=-1.0, origin_y=-0.5, width=60, height=40)
    )
    grid.cells[:, :] = -3.0
    grid.cells[0, :] = 3.0
    return grid


def _apply(m, x, y):
    m = np.array(m)
    return m @ np.array([x, y, 1.0])


@pytest.mark.parametrize("rotate", [0.0, 180.0, 90.0])
def test_matrices_are_inverse_and_follow_pose_frame(tmp_path, rotate) -> None:
    (tmp_path / "pose_frame.json").write_text(
        json.dumps({"rotate_deg": rotate, "translate_x": 4.0, "translate_y": 3.0}), encoding="utf-8"
    )
    zones = ZoneStore(("A",))
    zones.place(0.5, 0.2)
    png, meta = render(
        _grid(),
        zones=zones,
        zone_map=None,
        frame=PoseFrame.load(tmp_path),
        occ_thresh=1.0,
        free_thresh=-1.0,
        scale=2,
    )
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[1] == meta["width"] and image.shape[0] == meta["height"]
    for x, y in ((0.0, 0.0), (1.3, 0.7), (-0.8, 1.2)):
        u, v = _apply(meta["patrol_to_px"], x, y)
        back = _apply(meta["px_to_patrol"], u, v)
        assert back == pytest.approx([x, y], abs=1e-6)
        assert 0 <= u <= meta["width"] and 0 <= v <= meta["height"]
    # 벽 줄(행 0 = 순찰 y −0.5 근처)은 그림에서 어둡게 칠해진다.
    u, v = _apply(meta["patrol_to_px"], 0.0, -0.475)
    assert image[int(v), int(u)].max() < 100
    u, v = _apply(meta["patrol_to_px"], 0.0, 0.5)
    assert image[int(v), int(u)].min() > 200
    assert meta["zones"][0]["id"] == "A"


def test_north_up_for_rotated_frame(tmp_path) -> None:
    """순찰 +x 가 평면 −x(서쪽)면 그림에서 순찰 +x 는 왼쪽이다."""
    (tmp_path / "pose_frame.json").write_text(
        json.dumps({"rotate_deg": 180.0, "translate_x": 0.0, "translate_y": 0.0}), encoding="utf-8"
    )
    zones = ZoneStore(("A",))
    zones.place(0.0, 0.0)
    _png, meta = render(
        _grid(),
        zones=zones,
        zone_map=None,
        frame=PoseFrame.load(tmp_path),
        occ_thresh=1.0,
        free_thresh=-1.0,
    )
    u0, v0 = _apply(meta["patrol_to_px"], 0.0, 0.0)
    u1, v1 = _apply(meta["patrol_to_px"], 1.0, 0.0)
    assert u1 < u0 and v1 == pytest.approx(v0)
    u2, v2 = _apply(meta["patrol_to_px"], 0.0, 1.0)
    assert v2 > v0, "순찰 +y 는 평면 −y(남쪽) — 그림 아래"


def test_map_view_renders_once() -> None:
    calls = []

    def renderer():
        calls.append(1)
        return b"png", {"width": 1}

    view = MapView(renderer)
    assert view.get() == view.get()
    assert len(calls) == 1


def test_runtime_server_serves_the_white_dashboard() -> None:
    """흰 관제 화면(정본)이 운용 서버에서도 열린다 — 예전엔 /glass-preview/ 가 404 였다."""
    from fastapi.testclient import TestClient

    from host.dashboard.server import DEFAULT_STATIC_DIR, create_app
    from host.dashboard.state import DashboardState

    app = create_app(DashboardState("t", stale_after_ms=1000), static_dir=DEFAULT_STATIC_DIR)
    with TestClient(app) as http:
        page = http.get("/glass-preview/")
        assert page.status_code == 200 and "iframe" in page.text
        assert http.get("/glass-preview/theme.css").status_code == 200
        assert http.get("/index.html").status_code == 200
        first = http.get("/", follow_redirects=False)
        assert first.status_code in (302, 307) and first.headers["location"] == "/glass-preview/", (
            "첫 주소는 흰 화면 — 검정(앱 단독) 화면이 먼저 뜨지 않는다"
        )
        assert "glass-preview" in http.get("/index.html").text, (
            "단독으로 열면 흰 화면으로 넘기는 코드"
        )
