"""운용 계획 편집. 기존 개체 local.yaml·zones.json을 쓰며 로봇 명령은 보내지 않는다.

저장은 다음 런타임 시작에 적용된다. 실행 중 설정의 복사본과 저장본을 따로 보여 주어
저장 성공을 즉시 적용이나 주행 가능 판정으로 오해하지 않게 한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from host.behavior.planner import inflate, plan_to
from host.behavior.zones import ZoneStore
from host.common.config import (
    DEFAULT_CONFIG,
    DEFAULT_DEVICES_DIR,
    load_config,
    validate_base_config,
)
from host.slam.occupancy import OccupancyGrid
from host.slam.settings import maps_dir, plan_params_from_config

_FILES_LOCK = threading.RLock()


class PlanError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class ZoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,24}$")
    name: str = Field(default="", max_length=60)
    x: float | None = Field(default=None, ge=-10000, le=10000)
    y: float | None = Field(default=None, ge=-10000, le=10000)
    yaw_deg: float | None = Field(default=None, ge=-180, le=180)
    hazard: StrictBool = False
    helmet: StrictBool = True
    vest: StrictBool = True
    note: str = Field(default="", max_length=300)

    @field_validator("x", "y", "yaw_deg", mode="before")
    @classmethod
    def numbers_only(cls, value: Any) -> Any:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))):
            raise ValueError("좌표와 방향은 숫자로 입력하세요")
        return value

    @model_validator(mode="after")
    def paired_coordinates(self) -> ZoneInput:
        if (self.x is None) != (self.y is None):
            raise ValueError("X와 Y 좌표를 함께 입력하세요")
        if self.x is None and self.yaw_deg is not None:
            raise ValueError("방향을 지정하려면 먼저 위치를 정하세요")
        return self


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(default="", max_length=64)
    zones: list[ZoneInput] = Field(min_length=1, max_length=16)
    random_after_first_cycle: StrictBool = False

    @model_validator(mode="after")
    def unique_ids(self) -> PlanInput:
        if len({zone.id for zone in self.zones}) != len(self.zones):
            raise ValueError("구역 ID가 중복됐습니다")
        return self


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".planning-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


class PlanningService:
    def __init__(
        self,
        config: dict[str, Any],
        device: str,
        *,
        config_path: Path = DEFAULT_CONFIG,
        devices_dir: Path = DEFAULT_DEVICES_DIR,
        runtime_attached: bool = True,
    ) -> None:
        self.config = deepcopy(config)
        self.device = device
        self.config_path = config_path
        self.devices_dir = devices_dir
        self.local_path = devices_dir / f"{device}.local.yaml"
        self.maps = maps_dir(config)
        self.zones_path = self.maps / "zones.json"
        self.runtime_attached = runtime_attached
        self._active: dict[str, Any] | None
        try:
            self._active = self._plan(config)
        except (OSError, ValueError):
            self._active = None
        self._preview_lock = threading.Lock()

    def _saved_config(self) -> dict[str, Any]:
        saved = load_config(self.device, config_path=self.config_path, devices_dir=self.devices_dir)
        if maps_dir(saved) != self.maps:
            raise PlanError(
                "지도 폴더 설정이 바뀌었습니다. 서버를 다시 시작한 뒤 계획을 편집하세요.", 409
            )
        return saved

    def _revision(self) -> str:
        digest = hashlib.sha256()
        for path in (
            self.config_path,
            self.devices_dir / f"{self.device}.yaml",
            self.local_path,
            self.zones_path,
        ):
            digest.update(path.read_bytes() if path.is_file() else b"<absent>")
            digest.update(b"\0")
        # 지도가 교체되면 예전 좌표 초안과 경로도 다시 확인해야 한다. 큰 격자를
        # 요청마다 읽어 해시하지 않고 저장 파일의 세대(크기/수정 시각)를 포함한다.
        for name in ("slam_map.npy", "map_meta.json", "slam_map.pgm", "slam_map.yaml"):
            path = self.maps / name
            if path.is_file():
                stat = path.stat()
                digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()

    def _plan(self, config: dict[str, Any]) -> dict[str, Any]:
        section = config["zones"]
        store = ZoneStore.load(self.maps, section["ids"])
        policies = section.get("policies", {})
        zones = []
        for label in section["ids"]:
            anchor = store.get(label) if label in store else None
            policy = policies.get(label, {})
            zones.append(
                {
                    "id": label,
                    "name": policy.get("name", label),
                    "x": anchor.x if anchor else None,
                    "y": anchor.y if anchor else None,
                    "yaw_deg": None
                    if not anchor or anchor.yaw is None
                    else round(math.degrees(anchor.yaw), 2),
                    "hazard": label in section["hazard_ids"],
                    "helmet": policy.get("helmet", True),
                    "vest": policy.get("vest", True),
                    "note": policy.get("note", ""),
                }
            )
        return {"zones": zones, "random_after_first_cycle": section["random_after_first_cycle"]}

    def _grid(self) -> OccupancyGrid:
        try:
            grid = OccupancyGrid.load(self.maps)
        except (OSError, ValueError, KeyError) as exc:
            raise PlanError(
                "저장된 지도를 읽을 수 없습니다. 먼저 지도 파일을 준비하세요.", 404
            ) from exc
        if (
            grid.cells.size > 4_000_000
            or not np.isfinite(grid.cells).all()
            or not all(math.isfinite(v) for v in grid.extent)
            or grid.meta.resolution <= 0
        ):
            raise PlanError("지도 크기 또는 값이 편집기의 허용 범위를 벗어났습니다.")
        return grid

    def _map_info(self) -> dict[str, Any]:
        try:
            grid = self._grid()
        except PlanError as exc:
            return {"available": False, "reason": str(exc)}
        return {
            "available": True,
            "extent": list(grid.extent),
            "resolution": grid.meta.resolution,
            "width": int(grid.cells.shape[1]),
            "height": int(grid.cells.shape[0]),
        }

    def snapshot(self) -> dict[str, Any]:
        with _FILES_LOCK:
            saved = self._plan(self._saved_config())
            return {
                "device": self.device,
                "revision": self._revision(),
                "saved": saved,
                "active": self._active if self.runtime_attached else None,
                "pending_restart": self.runtime_attached and saved != self._active,
                "map": self._map_info(),
                "arrival_radius_m": self.config["zones"]["arrival_radius_mm"] / 1000,
                "mission_mode": self.config["mission"]["mode"],
                "writable": True,
            }

    def map_png(self) -> bytes:
        grid = self._grid()
        params = plan_params_from_config(self.config)
        pixels = np.full(grid.cells.shape, 220, dtype=np.uint8)
        pixels[grid.cells <= params.free_thresh] = 250
        pixels[grid.cells >= params.occ_thresh] = 62
        success, encoded = cv2.imencode(".png", np.flipud(pixels))
        if not success:
            raise PlanError("지도 이미지를 만들지 못했습니다.", 503)
        return encoded.tobytes()

    def preview(self, plan: PlanInput) -> dict[str, Any]:
        if not self._preview_lock.acquire(blocking=False):
            raise PlanError("경로를 계산 중입니다. 잠시 후 다시 확인하세요.", 409)
        try:
            revision = self._revision()
            if plan.revision != revision:
                raise PlanError(
                    "지도 또는 계획이 바뀌었습니다. 저장본을 다시 불러온 뒤 경로를 확인하세요.", 409
                )
            grid = self._grid()
            params = plan_params_from_config(self.config)
            blocked = inflate(grid, params)
            missing = [zone.id for zone in plan.zones if zone.x is None]
            if missing:
                raise PlanError("위치를 지정하지 않은 구역: " + ", ".join(missing))
            segments: list[dict[str, Any]] = []
            for start, end in zip(plan.zones, plan.zones[1:] + plan.zones[:1], strict=True):
                assert start.x is not None and start.y is not None
                assert end.x is not None and end.y is not None
                path = plan_to(end.id, (end.x, end.y), (start.x, start.y), grid, blocked, params)
                segments.append(
                    {
                        "from": start.id,
                        "to": end.id,
                        "reachable": path.reachable,
                        "length_m": round(path.length_m, 3) if path.reachable else None,
                        "points": list(path.waypoints) if path.reachable else [],
                    }
                )
            if self._revision() != revision:
                raise PlanError("계산 중 지도 또는 계획이 바뀌었습니다. 다시 불러오세요.", 409)
            return {
                "segments": segments,
                "all_reachable": all(s["reachable"] for s in segments),
                "total_m": round(sum(s["length_m"] or 0 for s in segments), 3),
                "scope": "저장 지도 위 첫 순회 경로 · 현재 로봇→첫 구역 이동과 실물 주행은 별도 확인",
            }
        finally:
            self._preview_lock.release()

    def save(self, plan: PlanInput) -> dict[str, Any]:
        with _FILES_LOCK:
            if plan.revision != self._revision():
                raise PlanError(
                    "다른 화면 또는 작업에서 설정이 바뀌었습니다. 새로 불러온 뒤 다시 저장하세요.",
                    409,
                )
            # 기존 로컬 센서/보행 설정은 보존하고 구역 설정만 덮어쓴다.
            local = (
                yaml.safe_load(self.local_path.read_text(encoding="utf-8"))
                if self.local_path.is_file()
                else {}
            )
            if not isinstance(local, dict):
                raise PlanError("개체 로컬 설정 형식이 올바르지 않습니다.")
            section = dict(local.get("zones") or {})
            section.update(
                ids=[z.id for z in plan.zones],
                hazard_ids=[z.id for z in plan.zones if z.hazard],
                random_after_first_cycle=plan.random_after_first_cycle,
                policies={
                    z.id: {
                        "name": z.name.strip() or z.id,
                        "helmet": z.helmet,
                        "vest": z.vest,
                        "note": z.note.strip(),
                    }
                    for z in plan.zones
                },
            )
            local["zones"] = section
            candidate = self._saved_config()
            candidate["zones"].update(section)
            validate_base_config(candidate)
            before = self.zones_path.read_bytes() if self.zones_path.exists() else None
            # 동일 지도 폴더를 다른 개체도 사용한다. 이번 개체에서 제거한 라벨의
            # 좌표는 보존한다(ZoneStore는 설정 ids만 읽는다). 명시적으로 위치를
            # 비운 구역만 공유 좌표에서 제거한다.
            anchors = json.loads(before) if before else {}
            if not isinstance(anchors, dict):
                raise PlanError("저장된 구역 좌표의 형식을 먼저 확인하세요.")
            for zone in plan.zones:
                anchors.pop(zone.id, None)
            anchors.update(
                {
                    z.id: {
                        "x": z.x,
                        "y": z.y,
                        **({"yaw": math.radians(z.yaw_deg)} if z.yaw_deg is not None else {}),
                    }
                    for z in plan.zones
                    if z.x is not None
                }
            )
            _atomic_write(
                self.zones_path, (json.dumps(anchors, ensure_ascii=False, indent=2) + "\n").encode()
            )
            try:
                _atomic_write(
                    self.local_path,
                    yaml.safe_dump(local, allow_unicode=True, sort_keys=False).encode(),
                )
            except OSError:
                if before is None:
                    self.zones_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.zones_path, before)
                raise
            return self.snapshot()
