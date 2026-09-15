"""씬 미리보기 — FPV + 전경 프레임을 파일로 저장해 품질을 확인한다.

C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/capture_preview.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

OUT = _ROOT / "sim" / "out"


def main() -> int:
    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True, "width": 1280, "height": 720})

    import numpy as np  # noqa: E402
    from isaacsim.core.api import World  # noqa: E402
    from isaacsim.sensors.camera import Camera  # noqa: E402

    from sim.factory_world import build_factory  # noqa: E402
    from sim.mechdog_proxy import attach_camera, build_robot  # noqa: E402
    from sim.people_spawner import spawn_workers  # noqa: E402

    world = World(stage_units_in_meters=1.0)
    stage = world.stage
    zones = build_factory(stage)
    robot_path = build_robot(stage)

    # 실험: 바닥 반사를 죽여 검정 대역 원인을 확인한다
    if "--mat-floor" in sys.argv:
        from sim.factory_world import _bind, _material

        mat = _material(
            stage, "/World/Factory/Looks/floor_matte", (0.35, 0.38, 0.42), roughness=0.95
        )
        for prim in stage.Traverse():
            if prim.GetName().startswith("SM_floor"):
                _bind(prim, mat)
        print("[preview] floor matte override applied")
    if "--with-people" in sys.argv:
        spawn_workers(stage, zones["person_zones"], count=6)
    fpv = attach_camera(robot_path)

    # 로봇을 스폰 지점으로 옮기고 통로를 따라 북쪽(+Y)을 보게 한다
    from pxr import Gf, UsdGeom  # noqa: E402

    sx, sy, _ = zones.get("robot_spawn", (0.0, 0.0, 0.0))
    rxf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
    rxf.AddTranslateOp().Set(Gf.Vec3d(sx, sy - 4.0, 0.0))
    rxf.AddRotateZOp().Set(90.0)

    # 전경 카메라 — 창고 남쪽 위에서 북쪽 통로를 본다. USD 카메라는 -Z 가 시선.
    overview = Camera(prim_path="/World/OverviewCam", resolution=(1280, 720))
    eye = np.array([-4.0, -12.0, 6.0])
    target = np.array([-4.0, 8.0, 0.5])
    f = (target - eye) / np.linalg.norm(target - eye)
    r = np.cross(f, np.array([0.0, 0.0, 1.0]))
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    m = np.stack([r, u, -f], axis=1)  # 열 기저: -Z → 시선
    tr = np.trace(m)
    s = np.sqrt(tr + 1.0) * 2.0
    quat = np.array(
        [s / 4.0, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    )
    overview.set_world_pose(position=eye, orientation=quat, camera_axes="usd")

    world.reset()
    fpv.initialize()
    overview.initialize()
    for _ in range(40):
        world.step(render=True)

    OUT.mkdir(parents=True, exist_ok=True)
    import cv2

    fpv_frame = fpv.get_rgba()
    if fpv_frame is not None:
        cv2.imwrite(
            str(OUT / "fpv_preview.png"), cv2.cvtColor(fpv_frame[..., :3], cv2.COLOR_RGB2BGR)
        )
        print(f"[preview] fpv -> {OUT / 'fpv_preview.png'}")

    ov_frame = overview.get_rgba()
    if ov_frame is not None:
        cv2.imwrite(
            str(OUT / "overview_preview.png"), cv2.cvtColor(ov_frame[..., :3], cv2.COLOR_RGB2BGR)
        )
        print(f"[preview] overview -> {OUT / 'overview_preview.png'}")
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
