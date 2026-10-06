"""실제 장소와 관계없는 합성 격자에서 동선 데이터·몸체 여유를 검사한다."""

import json

import pytest
from pydantic import ValidationError

from host.behavior.planner import PlanParams
from host.behavior.routes import Route, load_routes, route_digest, routes_content, validate_route
from host.slam.occupancy import OccupancyGrid


def route(**changes):
    return Route.model_validate(
        {
            "id": "test",
            "name": "합성 동선",
            "points": [{"x": -1, "y": 0}, {"x": 1, "y": 0, "aim_deg": 0, "dwell_s": 2}],
            **changes,
        }
    )


@pytest.fixture
def geometry():
    grid = OccupancyGrid.blank(resolution=0.1, span_cells=80)
    grid.cells.fill(-5)
    return grid, PlanParams(occ_thresh=1, free_thresh=-1, clearance_m=0.25, simplify_eps_m=0.1)


def test_roundtrip_keeps_order_optional_zero_aim_and_repeat(tmp_path):
    expected = route(repeat=0)
    assert load_routes(tmp_path) == {}
    (tmp_path / "routes.json").write_bytes(routes_content({expected.id: expected}))
    actual = load_routes(tmp_path)["test"]
    assert actual == expected
    assert actual.points[0].aim_deg is None and actual.points[1].aim_deg == 0
    assert actual.points[1].dwell_s == 2 and actual.repeat == 0
    assert route_digest(actual) == route_digest(expected)
    assert route_digest(route(repeat=1)) != route_digest(actual)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../escape"},
        {"name": "  "},
        {"repeat": True},
        {"repeat": 1.5},
        {"repeat": -1},
        {"points": []},
        {"points": [{"x": True, "y": 0}]},
        {"points": [{"x": 0, "y": float("nan")}]},
        {"points": [{"x": 0, "y": 0, "dwell_s": -1}]},
        {"points": [{"x": 0, "y": 0, "aim_deg": 181}]},
    ],
)
def test_invalid_route_is_rejected(changes):
    with pytest.raises(ValidationError):
        route(**changes)


def test_duplicate_id_file_is_rejected(tmp_path):
    entry = route().model_dump(mode="json")
    (tmp_path / "routes.json").write_text(json.dumps({"version": 1, "routes": [entry, entry]}))
    with pytest.raises(ValueError, match="중복"):
        load_routes(tmp_path)


def test_straight_wall_crossing_rejected_even_with_possible_detour(geometry):
    grid, params = geometry
    assert validate_route(route(), grid, params)["valid"]
    grid.cells[36:44, 40] = 5
    result = validate_route(route(), grid, params)
    assert not result["valid"] and result["invalid_points"] == []
    assert result["segments"][0]["from_index"] == 0
    assert result["segments"][0]["to_index"] == 1
    assert result["segments"][0]["reason"]


def test_body_radius_blocks_free_centerline_beside_unknown(geometry):
    grid, params = geometry
    grid.cells[41, 40] = 0
    result = validate_route(route(), grid, params)
    assert not result["valid"]
    assert not result["segments"][0]["valid"]


def test_repeat_validates_last_to_first_segment(geometry):
    grid, params = geometry
    grid.cells[38:42, 40] = 5
    points = [{"x": -1, "y": 0}, {"x": -1, "y": 1}, {"x": 1, "y": 1}, {"x": 1, "y": 0}]
    assert validate_route(route(points=points), grid, params)["valid"]
    result = validate_route(route(points=points, repeat=2), grid, params)
    assert not result["valid"]
    assert result["segments"][-1]["from_index"] == 3
    assert result["segments"][-1]["to_index"] == 0
    assert not result["segments"][-1]["valid"]


def test_single_point_and_map_edge_have_body_check(geometry):
    grid, params = geometry
    assert validate_route(route(points=[{"x": 0, "y": 0}]), grid, params)["valid"]
    for x in (3.95, 10000):
        result = validate_route(route(points=[{"x": x, "y": 0}]), grid, params)
        assert not result["valid"] and result["invalid_points"] == [0]
