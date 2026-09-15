"""공장 씬 스모크 — 씬·로봇·사람·카메라·프로토콜이 실제로 올라가는지 확인.

    C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/smoke_factory.py

몇 프레임만 돌리고 끝낸다. 카메라 프레임이 나오고, MOVE 명령에 로봇이
실제로 움직였는지만 본다 — 전체 구성 요소의 연결 검증이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True, "width": 640, "height": 480})

    import numpy as np  # noqa: E402
    from isaacsim.core.api import World  # noqa: E402

    from sim.factory_world import build_factory  # noqa: E402
    from sim.mechdog_proxy import (  # noqa: E402
        KinematicState,
        attach_camera,
        build_robot,
    )
    from sim.people_spawner import spawn_workers  # noqa: E402

    world = World(stage_units_in_meters=1.0)
    stage = world.stage
    zones = build_factory(stage)
    robot_path = build_robot(stage)
    workers = spawn_workers(stage, zones["person_zones"], count=4)
    camera = attach_camera(robot_path)
    kinematic = KinematicState()

    world.reset()
    camera.initialize()
    print(f"[smoke] workers spawned: {len(workers)}")

    # 30프레임 렌더 후 카메라 프레임 확인
    for _ in range(30):
        world.step(render=True)
    frame = camera.get_rgba()
    h, w = (frame.shape[0], frame.shape[1]) if frame is not None else (0, 0)
    nonempty = int(np.count_nonzero(frame[..., :3])) if frame is not None else 0
    print(f"[smoke] camera frame: {w}x{h}, nonzero px: {nonempty}")

    # MOVE 적용 → 자세 변화 확인
    for i in range(10):
        kinematic.apply("MOVE", 1000 + i * 100, step=60, angle=0)
        kinematic.step(0.1, 1000 + i * 100)
    print(f"[smoke] after MOVE 1s: x={kinematic.x:.3f} (expect ~0.104)")

    ok = h == 480 and w == 640 and nonempty > 0 and abs(kinematic.x - 0.104) < 0.01
    print(f"[smoke] {'PASS' if ok else 'FAIL'}")
    app.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
