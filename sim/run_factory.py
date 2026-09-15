"""공장 씬 실행 — 환경 A 의 진입점.

    C:/Users/a9800/isaac_clean/venv/Scripts/python.exe sim/run_factory.py
    [--headless] [--workers 6] [--stream 8080] [--cmd-port 5001]

일어나는 일:
  ① 공장 씬을 쌓는다 (factory_world)
  ② 실측 치수의 MechDog 프록시 + 15cm FPV 카메라를 얹는다
  ③ PPE 착용 4조합의 근로자를 배치한다
  ④ UDP 프로토콜 서버를 연다 — 실기와 같은 명령을 받아 시뮬 로봇이 움직인다
  ⑤ 카메라 프레임을 MJPEG 웹 스트림으로 보낸다

이 상태에서 기존 도구는 **IP 만 바꾸면** 그대로 붙는다:
  python tools/teleop.py --robot 127.0.0.1        ← 시뮬 로봇을 텔레옵으로
  python tools/mechdog_command.py --robot 127.0.0.1 ...
"""

from __future__ import annotations

import argparse

# cp949 콘솔에서도 살아남게 — 지원 못 하는 문자는 깨진 글자가 아니라 치환한다
import contextlib
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(errors="replace")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--stream", type=int, default=8080)
    parser.add_argument("--room", type=str, default="18,12,5", help="공장 크기 m — 가로,세로,높이")
    parser.add_argument(
        "--spawn", type=str, default=None, help="로봇 시작 자세 — x,y,yaw(도). 생략 시 씬 기본 스폰"
    )
    args = parser.parse_args(argv)

    room_m = tuple(float(v) for v in args.room.split(","))

    from isaacsim import SimulationApp

    app = SimulationApp({"headless": args.headless, "width": 640, "height": 480})

    # 조용한 종료 진단 — 스레드 예외도 파일로 남긴다 (스트림 핸들러 등)
    import threading

    import numpy as np  # noqa: E402
    from isaacsim.core.api import World  # noqa: E402
    from pxr import Gf, UsdGeom  # noqa: E402

    from sim.factory_world import build_factory  # noqa: E402
    from sim.mechdog_proxy import (  # noqa: E402
        KinematicState,
        attach_camera,
        build_robot,
    )
    from sim.people_spawner import spawn_workers  # noqa: E402
    from sim.protocol_server import SimProtocolServer  # noqa: E402
    from sim.web_stream import MjpegServer  # noqa: E402

    _crash = _ROOT / "sim" / "out" / "sim_crash.log"

    def _thread_hook(targs):
        _crash.write_text(
            f"thread {targs.thread.name}: {targs.exc_type.__name__}: {targs.exc_value}\n",
            encoding="utf-8",
        )

    threading.excepthook = _thread_hook

    world = World(stage_units_in_meters=1.0)
    stage = world.stage

    zones = build_factory(stage, room_m=room_m)
    robot_path = build_robot(stage)
    workers = spawn_workers(stage, zones["person_zones"], count=args.workers)
    camera = attach_camera(robot_path)

    if args.spawn:
        sx, sy, syaw = (float(v) for v in args.spawn.split(","))
    else:
        sx, sy = zones.get("robot_spawn", (0.0, 0.0))[:2]
        syaw = 90.0  # 창고 통로를 따라 북쪽을 본다
    kinematic = KinematicState(x=sx, y=sy, yaw=np.deg2rad(syaw))
    server = SimProtocolServer(kinematic)
    stream = MjpegServer(port=args.stream)
    stream.start()

    world.reset()
    camera.initialize()

    robot_xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
    print(
        f"[sim] 공장 씬 준비 — 로봇 {robot_path}, 근로자 {len(workers)}명, "
        f"명령 :5001 · 텔레메트리 :5101"
    )
    print("[sim] 조작: 기존 도구에서 --robot 127.0.0.1 로 붙으면 됩니다")

    last = time.perf_counter()
    crash_log = _ROOT / "sim" / "out" / "sim_crash.log"
    frames = 0
    try:
        while app.is_running():
            world.step(render=True)
            now_ms = int(time.time() * 1000)
            dt = min(time.perf_counter() - last, 0.1)
            last = time.perf_counter()

            server.poll(now_ms)
            kinematic.step(dt, now_ms)
            server.emit_telemetry(now_ms)

            # 키네마틱 자세 → prim 반영
            ops = robot_xf.GetOrderedXformOps()
            if not ops:
                robot_xf.AddTranslateOp().Set(Gf.Vec3d(kinematic.x, kinematic.y, 0.0))
                robot_xf.AddRotateZOp().Set(np.rad2deg(kinematic.yaw))
            else:
                for op in ops:
                    if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                        op.Set(Gf.Vec3d(kinematic.x, kinematic.y, 0.0))
                    elif op.GetOpType() == UsdGeom.XformOp.TypeRotateZ:
                        op.Set(np.rad2deg(kinematic.yaw))

            frame = camera.get_rgba()
            if frame is not None and getattr(frame, "size", 0):
                stream.update(frame[..., :3])
            frames += 1
    except KeyboardInterrupt:
        crash_log.write_text("KeyboardInterrupt\n", encoding="utf-8")
    except BaseException:  # noqa: BLE001 — 조용한 종료의 원인을 잡는다
        import traceback

        crash_log.write_text(f"frames={frames}\n{traceback.format_exc()}\n", encoding="utf-8")
    else:
        crash_log.write_text(
            f"loop exited cleanly: is_running()=False after {frames} frames\n", encoding="utf-8"
        )
    finally:
        stream.stop()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
