"""fallen 자세 수치 검증 — 베이크된 정점 z 범위 + z_off 적용 후 월드 높이."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import random

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": 640, "height": 480})

import cv2  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from pxr import Gf, Usd, UsdGeom  # noqa: E402

from sim.factory_world import build_factory  # noqa: E402
from sim.gen_ppe_dataset import _apply_pose  # noqa: E402
from sim.mechdog_proxy import attach_camera, build_robot  # noqa: E402
from sim.people_spawner import spawn_workers  # noqa: E402

world = World(stage_units_in_meters=1.0)
stage = world.stage
zones = build_factory(stage)
robot_path = build_robot(stage)
workers = spawn_workers(stage, [(-6, 6, -3, 3)], count=1, seed=3, all_parts=True)
camera = attach_camera(robot_path)
world.reset()
camera.initialize()

w = workers[0]
rng = random.Random(5)
ok = _apply_pose(stage, w, rng, 0, force_pose="fallen")
print(f"[pose] ok={ok} z_off={w.get('_z_off', 0):.3f}")

# 베이크된 정점의 char-local z 범위
import numpy as np

zs = []
for prim in stage.Traverse():
    p = str(prim.GetPath())
    if not p.startswith(w["path"] + "/char/"):
        continue
    mesh = UsdGeom.Mesh(prim)
    if not mesh:
        continue
    pts = mesh.GetPointsAttr().Get()
    if pts is not None and len(pts):
        arr = np.array(pts)
        zs.append((arr[:, 2].min(), arr[:, 2].max()))
print("[verts] char-local z:", [(f"{a:.2f}", f"{b:.2f}") for a, b in zs])

# 텔레포트 + z_off 적용 후 실제 월드 위치 (루트 prim)
api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(w["path"]))
api.SetTranslate(Gf.Vec3d(0.0, 0.0, w.get("_z_off", 0.0)))
api.SetRotate((0.0, 0.0, 0.0), UsdGeom.XformCommonAPI.RotationOrderZYX)

xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
xf.AddTranslateOp().Set(Gf.Vec3d(-3.0, -3.0, 0.0))
xf.AddRotateZOp().Set(45.0)

for _ in range(40):
    world.step(render=True)

# 텔레포트가 z 를 유지하는지 + 이동 후에도 바닥에 붙는지
m = UsdGeom.Xformable(stage.GetPrimAtPath(w["path"])).ComputeLocalToWorldTransform(
    Usd.TimeCode.Default())
print(f"[world] worker root translate = {m.ExtractTranslation()}")

# 두 번째 텔레포트 (gen 루프처럼)
api.SetTranslate(Gf.Vec3d(2.0, 2.0, w.get("_z_off", 0.0)))
for _ in range(15):
    world.step(render=True)
rgba = camera.get_rgba()
rgb = rgba[..., :3]
cv2.imwrite("run-logs/dbg_fallen.jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
m2 = UsdGeom.Xformable(stage.GetPrimAtPath(w["path"])).ComputeLocalToWorldTransform(
    Usd.TimeCode.Default())
print(f"[world] after teleport2 = {m2.ExtractTranslation()}")
print("[dbg] saved run-logs/dbg_fallen.jpg")
app.close()
