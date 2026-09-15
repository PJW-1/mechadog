"""자세 라이브러리 검증 — 6종 자세를 강제로 굽히고 한 화면에 렌더."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import random

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": 640, "height": 480})

import cv2  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from pxr import Gf, UsdGeom  # noqa: E402

from sim.factory_world import build_factory  # noqa: E402
from sim.gen_ppe_dataset import _apply_pose  # noqa: E402
from sim.mechdog_proxy import attach_camera, build_robot  # noqa: E402
from sim.people_spawner import spawn_workers  # noqa: E402

world = World(stage_units_in_meters=1.0)
stage = world.stage
zones = build_factory(stage)
robot_path = build_robot(stage)

POSES = ["stand", "bend", "reach", "squat", "stride", "fallen"]
workers = spawn_workers(
    stage, [(-6, 6, -3, 3)] * 6, count=6, seed=3, all_parts=True
)
camera = attach_camera(robot_path)
world.reset()
camera.initialize()

rng = random.Random(5)
# 발·발가락 관절명 확인
from pxr import UsdSkel
for prim in stage.Traverse():
    if prim.GetTypeName() == "Skeleton":
        js = [str(x) for x in UsdSkel.Skeleton(prim).GetJointsAttr().Get()]
        print("[joints]", [j.rsplit("/",1)[-1] for j in js
              if any(k in j.lower() for k in ("foot","toe","leg","thigh","calf","ball"))])
        break

# 6명을 일렬로 배치 — 자세별 비교
for i, w in enumerate(workers):
    api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(w["path"]))
    api.SetTranslate(Gf.Vec3d(-5.0 + i * 2.0, 0.0, 0.0))
    api.SetRotate((0.0, 0.0, 0.0), UsdGeom.XformCommonAPI.RotationOrderZYX)
    ok = _apply_pose(stage, w, rng, i, force_pose=POSES[i])
    # z_off 는 _apply_pose 가 계산 — translate 에 반영
    api.SetTranslate(Gf.Vec3d(-5.0 + i * 2.0, 0.0, w.get("_z_off", 0.0)))
    print(f"[pose] w{i} {POSES[i]}: ok={ok} z_off={w.get('_z_off', 0):.2f}")

# 로봇을 정면에 두고 일렬을 바라보게
xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
xf.AddTranslateOp().Set(Gf.Vec3d(0.0, -7.0, 0.0))
xf.AddRotateZOp().Set(90.0)

for _ in range(40):
    world.step(render=True)

rgba = camera.get_rgba()
rgb = rgba[..., :3]
cv2.imwrite("run-logs/dbg_poses.jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
print("[dbg] saved run-logs/dbg_poses.jpg")
app.close()
