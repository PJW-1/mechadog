"""합성 PPE 데이터셋 생성 — 공장 씬을 무작위화하며 FPV 프레임 + YOLO 라벨을 뽑는다.

실사 촬영(`tools/ppe_seed_labels.py`가 초안을 다는 쪽)과 역할이 다르다 — 여기서
나오는 라벨은 **스폰 정답 그 자체**다. 어떤 작업자가 안전모·조끼를 입었는지
씬을 만든 우리가 알고 있으므로 색 추정이 아니라 부위 prim 의 투영 박스를 쓴다.

라벨 규약은 `tools/ppe_seed_labels.py` 와 동일하다:

    0 helmet · 1 no_helmet · 2 vest · 3 no_vest   (person 은 학습 대상 아님, ADR-12)

부위 매핑 — 폴백 인형은 머리/몸통/안전모/조끼가 별도 prim 이라 부위 박스가
정확히 나온다. 실제 캐릭터 에셋(부위 prim 없음)은 사람 전체 박스를 초안
라벨러와 같은 비율(머리 상단 25%, 몸통 25~80%)로 나눠 근사한다.

출력 — `datasets/ppe/images/train/<session>/` + `labels/train/<session>/`.
**합성 데이터는 train 에만 둔다** — val/test 는 실사여야 "시뮬에서만 잘 맞는
모델"을 조기에 걸러낸다. 분할 규칙은 `datasets/README.md` 참조.

실행 (Isaac venv):

    C:/Users/a9800/isaac_clean/venv/Scripts/python.exe \\
        sim/gen_ppe_dataset.py --count 50 --preview datasets/ppe/preview/train
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 한국어 Windows 콘솔(cp949)은 `—`·이모지에서 죽는다 — kit 이 표준출력을
# 감싸도 근본 스트림의 errors 정책은 유지되므로 먼저 치환으로 낮춘다.
for _s in (sys.stdout, sys.stderr):
    if (rc := getattr(_s, "reconfigure", None)) is not None:
        rc(errors="replace")

#: `tools/ppe_seed_labels.py` 의 CLASS_IDS 와 반드시 일치시킨다.
CLASS_IDS = {"helmet": 0, "no_helmet": 1, "vest": 2, "no_vest": 3}

#: 부위 박스는 prim 바운드가 아니라 **작업자의 알려진 위치 + 표준 인체 비율**로
#: 만든다 — 참조 캐릭터의 ComputeWorldBound 는 스폰 위치를 반영하지 않는
#: 로컬 바운드를 돌려주는 함정이 있어 믿을 수 없다 (실측 확인됨).
#: (z0, z1, 반폭) — 170cm 성인. 폴백 인형과 캐릭터 에셋 둘 다 이 비율 안에 든다.
HEAD_ZONE = (1.48, 1.78, 0.17)
TORSO_ZONE = (0.80, 1.42, 0.26)

#: 이 픽셀보다 작은 투영 박스는 라벨하지 않는다 — 학습에 잡음만 된다.
MIN_BOX_PX = 8


def _quat_to_mat(q):
    """wxyz 쿼터니언 → 3x3 회전행렬."""
    import numpy as np

    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def _project_box(corners, cam_pos, rot_inv, k, size):
    """월드 좌표 꼭짓점들 → (x1, y1, x2, y2) 픽셀 박스. 전부 카메라 뒤면 None.

    camera_axes="world" 규약 — 카메라 +X 가 시선, +Y 좌, +Z 위. 따라서
    화면 오른쪽은 -Y, 아래는 -Z 다.
    """
    import numpy as np

    w, h = size
    fx, fy, cx, cy = k[0, 0], k[1, 1], k[0, 2], k[1, 2]
    us, vs = [], []
    for p in corners:
        c = rot_inv @ (np.asarray(p, dtype=float) - cam_pos)
        if c[0] <= 0.03:  # near-plane 뒤는 클램프 — 프레임 끝까지 늘어난다
            c = np.array([0.03, c[1], c[2]])
        us.append(cx - fx * c[1] / c[0])
        vs.append(cy - fy * c[2] / c[0])
    x1, x2 = max(0.0, min(us)), min(w - 1.0, max(us))
    y1, y2 = max(0.0, min(vs)), min(h - 1.0, max(vs))
    if x2 - x1 < MIN_BOX_PX or y2 - y1 < MIN_BOX_PX:
        return None
    return (x1, y1, x2, y2)


def _zone_corners(wx: float, wy: float, zone: tuple):
    """작업자 위치 (wx, wy) 에 세운 부위 박스의 8 꼭짓점 — 방향 무관하게 사방으로."""
    z0, z1, half = zone
    return [(wx + dx, wy + dy, z) for dx in (-half, half) for dy in (-half, half) for z in (z0, z1)]


def _center_depth(corners, cam_pos, rot_inv):
    """박스 중심의 카메라 전방 거리(m)."""
    import numpy as np

    c = np.mean(np.asarray(corners, dtype=float), axis=0)
    return float((rot_inv @ (c - cam_pos))[0])


def _occluded(box, part_depth, depth_img):
    """박스 중심 픽셀의 씬 깊이가 부위보다 유의미하게 가까우면 가려진 것."""
    x1, y1, x2, y2 = box
    u, v = int((x1 + x2) / 2), int((y1 + y2) / 2)
    h, w = depth_img.shape[:2]
    if not (0 <= u < w and 0 <= v < h):
        return True
    scene_d = float(depth_img[v, u])
    # 깊이 유효치가 없거나(하늘 등) 부위가 더 가까우면 보인다
    return scene_d > 0 and scene_d < part_depth - 0.25


def _yolo(cls, box, size):
    w, h = size
    x1, y1, x2, y2 = box
    return (
        f"{cls} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
        f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}"
    )


def _set_visible(prim, on: bool) -> None:
    from pxr import UsdGeom  # noqa: N806

    img = UsdGeom.Imageable(prim)
    img.MakeVisible() if on else img.MakeInvisible()


def _find_ppe_prims(stage, worker_path: str) -> dict[str, list]:
    """작업자 아래에서 PPE 부위 prim 을 찾는다.

    폴백 인형은 `char/helmet`·`char/vest` 직계 자식, 캐릭터 에셋은 메시 이름에
    `hardhat`·`safetyvest` 가 들어간다 (male_adult_construction_05 기준).
    찾은 prim 의 가시성을 껐다 켜서 4조합을 만든다 — 둘 다 없으면 그 작업자는
    조합을 바꿀 수 없어 스폰 복장이 정답이다.
    """
    out: dict[str, list] = {"helmet": [], "vest": []}
    base = worker_path + "/char"
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if not path.startswith(base + "/"):
            continue
        name = path.rsplit("/", 1)[-1].lower()
        if "hardhat" in name or name == "helmet":
            out["helmet"].append(prim)
        elif "safetyvest" in name or name == "vest":
            out["vest"].append(prim)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=50, help="생성 프레임 수")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument(
        "--session",
        type=str,
        default="sim_factory",
        help="세션명 — images|labels/train/<session>/ 아래에 쌓인다",
    )
    p.add_argument("--out", type=str, default=str(_ROOT / "datasets" / "ppe"))
    p.add_argument(
        "--preview", type=str, default=None, help="박스를 그린 검수용 이미지 저장 디렉터리"
    )
    p.add_argument("--settle", type=int, default=2, help="무작위화 후 렌더 안정화 스텝 수")
    args = p.parse_args(argv)

    rng = random.Random(args.seed)
    out = Path(args.out)
    img_dir = out / "images" / "train" / args.session
    lbl_dir = out / "labels" / "train" / args.session
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)
    if args.preview:
        Path(args.preview).mkdir(parents=True, exist_ok=True)

    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True, "width": 640, "height": 480})

    import cv2
    from isaacsim.core.api import World
    from pxr import Gf, UsdGeom  # noqa: N806

    from sim.factory_world import build_factory
    from sim.mechdog_proxy import attach_camera, build_robot
    from sim.people_spawner import spawn_workers

    world = World(stage_units_in_meters=1.0)
    stage = world.stage
    zones = build_factory(stage)
    robot_path = build_robot(stage)
    # 모든 부위를 달고 스폰해 프레임마다 가시성으로 4조합을 만든다
    workers = spawn_workers(
        stage, zones["person_zones"], count=args.workers, seed=args.seed, all_parts=True
    )
    camera = attach_camera(robot_path)
    world.reset()
    camera.initialize()
    # 가림 판정에 쓸 깊이 — 부착하지 않으면 get_depth() 가 None 을 돌려준다
    camera.add_distance_to_image_plane_to_frame()

    # 작업자별 토글 가능한 PPE prim 목록 — 부위 prim 이 있으면 조합 무작위화 가능
    for wrec in workers:
        wrec["ppe"] = _find_ppe_prims(stage, wrec["path"])

    robot_xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
    walk = zones["walkable"]  # (x0, x1, y0, y1)
    size = camera.get_resolution()  # (w, h)

    manifest = out / "labels" / "train" / args.session / "_manifest.csv"
    mfile = manifest.open("w", newline="", encoding="utf-8")
    mw = csv.writer(mfile)
    mw.writerow(["file", "robot_x", "robot_y", "robot_yaw_deg", "combos", "n_labels"])

    print(f"[gen] 씬 준비 — 작업자 {len(workers)}명, 목표 {args.count}프레임")
    t0 = time.time()
    made = 0
    attempts = 0
    try:
        # 초기 프레임은 rgba 가 비어 올 수 있다 — 성공 기준으로 센다
        while made < args.count and attempts < args.count + 30:
            attempts += 1
            # ── 작업자 무작위화: 위치·방향·착용 조합(가시성 토글) ──
            combos = []
            for wrec in workers:
                zone = zones["person_zones"][rng.randrange(len(zones["person_zones"]))]
                wx = rng.uniform(zone[0], zone[1])
                wy = rng.uniform(zone[2], zone[3])
                wyaw = rng.uniform(0.0, 360.0)
                api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(wrec["path"]))
                api.SetTranslate(Gf.Vec3d(wx, wy, 0.0))
                api.SetRotate((0.0, 0.0, wyaw), UsdGeom.XformCommonAPI.RotationOrderZYX)
                if wrec["ppe"]["helmet"] or wrec["ppe"]["vest"]:
                    # 토글 가능한 부위가 있으면 조합을 무작위화한다 — 없으면
                    # 스폰 복장이 정답 (임의 주장은 거짓 라벨이 된다)
                    combo = rng.choice([("helmet", "vest"), ("helmet",), ("vest",), ()])
                    for part, prims in wrec["ppe"].items():
                        for prim in prims:
                            _set_visible(prim, part in combo)
                else:
                    combo = wrec["wears"]
                tag = "f" if wrec["asset"] == "fallback" else "c"
                combos.append(f"{tag}:" + ("+".join(combo) or "none"))
                wrec["combo"] = combo
                wrec["pos"] = (wx, wy)

            # ── 로봇 포즈 — 실측 기하(FOV 74°×59°, 높이 0.15m, 하향 7°)로
            #    머리는 ~3.6m, 몸통은 ~2.3m, 온몸은 ~4m 부터 프레임에 들어온다.
            #    (⚠️ 옛 32° 화각 기준 "머리 17m" 는 오류값의 산물이었다.)
            #    70%는 PPE 가 보이는 실효 거리 2.5~8m 에서 작업자를 바라보게,
            #    15%는 시연 거리 근접(1.2~2.5m — 조끼만 보이는 것이 실제 물리),
            #    15%는 완전 무작위(부정 배경 프레임도 데이터다) ──
            focus = rng.choice(workers)
            roll = rng.random()
            if roll < 0.85:
                ang = rng.uniform(0, 2 * math.pi)
                dist = rng.uniform(2.5, 8.0) if roll < 0.70 else rng.uniform(1.2, 2.5)
                rx = focus["pos"][0] - math.cos(ang) * dist
                ry = focus["pos"][1] - math.sin(ang) * dist
                rx = min(max(rx, walk[0] + 0.5), walk[1] - 0.5)
                ry = min(max(ry, walk[2] + 0.5), walk[3] - 0.5)
                ryaw = math.degrees(math.atan2(focus["pos"][1] - ry, focus["pos"][0] - rx))
                ryaw += rng.uniform(-20.0, 20.0)
            else:
                rx = rng.uniform(walk[0] + 0.5, walk[1] - 0.5)
                ry = rng.uniform(walk[2] + 0.5, walk[3] - 0.5)
                ryaw = rng.uniform(0.0, 360.0)
            ops = robot_xf.GetOrderedXformOps()
            if not ops:  # 첫 프레임은 op 가 없다 — 만들어 둔다
                robot_xf.AddTranslateOp().Set(Gf.Vec3d(rx, ry, 0.0))
                robot_xf.AddRotateZOp().Set(ryaw)
            else:
                for op in ops:
                    if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                        op.Set(Gf.Vec3d(rx, ry, 0.0))
                    elif op.GetOpType() == UsdGeom.XformOp.TypeRotateZ:
                        op.Set(ryaw)

            for _ in range(args.settle):  # 이동 반영 + 렌더 안정화
                world.step(render=True)

            rgba = camera.get_rgba()
            if rgba is None or not getattr(rgba, "size", 0):
                continue
            rgb = rgba[..., :3]
            depth = camera.get_depth()
            cam_pos, cam_quat = camera.get_world_pose("world")
            rot_inv = _quat_to_mat(cam_quat).T
            k = camera.get_intrinsics_matrix()

            # ── 라벨 계산 — 위치 기반 부위 박스를 카메라로 투영 ──
            lines: list[str] = []
            boxes_for_preview: list[tuple[tuple, int]] = []
            for wrec in workers:
                combo = wrec["combo"]
                wx, wy = wrec["pos"]
                for part, cls_w, cls_b, zone in (
                    ("helmet", 0, 1, HEAD_ZONE),
                    ("vest", 2, 3, TORSO_ZONE),
                ):
                    corners = _zone_corners(wx, wy, zone)
                    box = _project_box(corners, cam_pos, rot_inv, k, size)
                    if box is None:
                        continue
                    pd = _center_depth(corners, cam_pos, rot_inv)
                    if pd <= 0:  # 전부 카메라 뒤
                        continue
                    if depth is not None and _occluded(box, pd, depth):
                        continue
                    cls = cls_w if part in combo else cls_b
                    lines.append(_yolo(cls, box, size))
                    boxes_for_preview.append((box, cls))

            name = f"{args.session}_{made:05d}"
            img_path = img_dir / f"{name}.jpg"
            ok, buf = cv2.imencode(
                ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            if not ok:
                continue
            img_path.write_bytes(buf.tobytes())
            (lbl_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            mw.writerow(
                [
                    f"{name}.jpg",
                    f"{rx:.2f}",
                    f"{ry:.2f}",
                    f"{ryaw:.1f}",
                    ";".join(combos),
                    len(lines),
                ]
            )

            if args.preview:
                vis = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
                colors = {0: (0, 140, 255), 1: (0, 0, 220), 2: (0, 220, 0), 3: (160, 160, 0)}
                for box, cls in boxes_for_preview:
                    x1, y1, x2, y2 = (int(v) for v in box)
                    cv2.rectangle(vis, (x1, y1), (x2, y2), colors[cls], 1)
                    cv2.putText(
                        vis,
                        str(cls),
                        (x1, max(10, y1 - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.35,
                        colors[cls],
                        1,
                    )
                cv2.imwrite(str(Path(args.preview) / f"{name}.jpg"), vis)
            made += 1
            if made % 10 == 0:
                print(f"[gen] {made}/{args.count} — {time.time() - t0:.0f}s 경과")
    finally:
        mfile.close()
        app.close()

    print(f"[gen] 완료 — {made}프레임, 라벨·매니페스트는 {lbl_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
