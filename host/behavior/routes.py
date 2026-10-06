"""사용자가 그린 순서 있는 동선과 저장 지도상의 직선 충돌 검사."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from host.behavior.planner import PlanParams, body_collision_mask, inflate, segment_clear
from host.slam.occupancy import OccupancyGrid

ROUTES_FILENAME = "routes.json"
MAX_ROUTES = 64


class RoutePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    x: float = Field(ge=-10000, le=10000)
    y: float = Field(ge=-10000, le=10000)
    aim_deg: float | None = Field(default=None, ge=-180, le=180)
    dwell_s: float | None = Field(default=None, ge=0, le=3600)
    search_deg: float | None = Field(default=None, ge=0, le=60)
    label: str | None = Field(default=None, max_length=60)

    @field_validator("x", "y", "aim_deg", "dwell_s", "search_deg", mode="before")
    @classmethod
    def numbers_only(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))):
            raise ValueError("좌표·방향·머무는 시간은 숫자로 입력하세요")
        return value


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    points: tuple[RoutePoint, ...] = Field(min_length=1, max_length=256)
    repeat: StrictInt = Field(default=1, ge=0, le=9999)

    @field_validator("name")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("동선 이름을 입력하세요")
        return value.strip()


def load_routes(directory: Path) -> dict[str, Route]:
    """없는 저장소는 빈 목록, 손상된 저장소는 거절한다. 순서는 보존한다."""
    path = directory / ROUTES_FILENAME
    if not path.exists():
        return {}
    if path.stat().st_size > 4_000_000:
        raise ValueError("저장된 동선 파일이 허용 크기를 넘었습니다")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("routes"), list)
        or len(data["routes"]) > MAX_ROUTES
    ):
        raise ValueError("저장된 동선 형식이 올바르지 않습니다")
    routes = [Route.model_validate(value) for value in data["routes"]]
    if len({route.id for route in routes}) != len(routes):
        raise ValueError("저장된 동선 ID가 중복됐습니다")
    return {route.id: route for route in routes}


def routes_content(routes: dict[str, Route]) -> bytes:
    data = {"version": 1, "routes": [route.model_dump(mode="json") for route in routes.values()]}
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def route_digest(route: Route) -> str:
    """확인창에서 본 동선과 실행할 불변 사본이 같은지 확인하는 내용 지문."""
    content = json.dumps(
        route.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def validate_route(
    route: Route,
    grid: OccupancyGrid,
    params: PlanParams,
    *,
    obstacle_grid: OccupancyGrid | None = None,
) -> dict[str, Any]:
    """그린 선 자체를 검사한다. 돌아가는 A* 경로가 있어도 벽 통과 선은 거절한다.

    반복 순회에는 마지막→첫 지점도 포함한다. 현재 위치→첫 지점은 런타임 goto가
    별도로 검사한다. 기존 계획 여유와 몸체 반경·미관측·지도 가장자리를 모두 지킨다.
    """
    blocked = inflate(grid, params, obstacle_grid=obstacle_grid) | body_collision_mask(
        grid, params, obstacle_grid=obstacle_grid
    )
    points = [(point.x, point.y) for point in route.points]
    invalid_points = [
        index
        for index, point in enumerate(points)
        if not grid.inside(*grid.to_cell(*point)) or not segment_clear(grid, blocked, point, point)
    ]
    pairs = [(index, index + 1) for index in range(len(points) - 1)]
    if route.repeat != 1 and len(points) > 1:
        pairs.append((len(points) - 1, 0))
    segments: list[dict[str, Any]] = []
    for start, end in pairs:
        clear = (
            start not in invalid_points
            and end not in invalid_points
            and segment_clear(grid, blocked, points[start], points[end])
        )
        segments.append(
            {
                "from_index": start,
                "to_index": end,
                "valid": clear,
                "reason": "" if clear else "벽·미관측 영역 또는 로봇 몸체 여유를 침범합니다",
                "points": [list(points[start]), list(points[end])],
                "length_m": round(math.dist(points[start], points[end]), 3),
            }
        )
    valid = not invalid_points and all(segment["valid"] for segment in segments)
    return {
        "valid": valid,
        "segments": segments,
        "invalid_points": invalid_points,
        "total_m": round(sum(segment["length_m"] for segment in segments), 3),
        "reason": "" if valid else "빨간 구간·지점을 이동하세요. 벽과 로봇 몸체 여유를 확인하세요.",
        "scope": "저장 지도 위 동선 · 현재 위치에서 첫 지점까지의 이동은 시작 시 다시 확인",
    }
