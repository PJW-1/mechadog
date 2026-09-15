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
HEAD_ZONE = (1.50, 1.75, 0.16)
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


def _floor_probe():
    """(x, y) 가 열린 바닥인지 — 위에서 내려쏴 z>0.35 지오메트리에 맞으면 랙 안."""
    try:
        import carb
        from omni.physx import get_physx_scene_query_interface

        sq = get_physx_scene_query_interface()

        def clear(x: float, y: float) -> bool:
            try:
                hit = sq.raycast_closest(carb.Float3(x, y, 2.6), carb.Float3(0.0, 0.0, -1.0), 5.0)
            except Exception:
                return True
            if not hit or not hit.get("hit"):
                return True
            return float(hit["position"][2]) < 0.35

        return clear
    except Exception:
        return lambda *_args: True


def _zone_corners(wx: float, wy: float, zone: tuple):
    """작업자 위치 (wx, wy) 에 세운 부위 박스의 8 꼭짓점 — 방향 무관하게 사방으로."""
    z0, z1, half = zone
    return [(wx + dx, wy + dy, z) for dx in (-half, half) for dy in (-half, half) for z in (z0, z1)]


def _char_world_xy(stage, worker_path: str):
    """보이는 몸통의 실제 월드 위치 — 캐릭터 에셋은 메시가 루트에서 어긋나 있어
    worker 루트 translate 를 그대로 쓰면 박스가 빈 공간에 뜬다 (실측 확인).
    char 의 월드 바운딩박스 중심을 쓰고, 못 재면 prim 변환으로 폴백한다."""
    from pxr import Usd, UsdGeom  # noqa: N806

    # ComputeWorldBound 는 스킨드 메시의 디포머를 바인드 자세로 재평가해
    # 입혀둔 팔 내림 애니메이션을 풀어버린다 (실측 확인) — 바운드 대신
    # char prim 의 월드 변환만 쓴다 (루트 대비 오프셋 ~2cm로 충분히 정확).
    prim = stage.GetPrimAtPath(worker_path + "/char")
    if not prim or not prim.IsValid():
        prim = stage.GetPrimAtPath(worker_path)
    m = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    t = m.ExtractTranslation()
    return float(t[0]), float(t[1])


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


def _rot4(axis: str, deg: float):
    """축·각도 → 4x4 회전행렬 (numpy)."""
    import numpy as np

    a = np.deg2rad(deg)
    c, s = np.cos(a), np.sin(a)
    r = np.eye(4)
    if axis == "X":
        r[1, 1], r[1, 2], r[2, 1], r[2, 2] = c, -s, s, c
    elif axis == "Y":
        r[0, 0], r[0, 2], r[2, 0], r[2, 2] = c, s, -s, c
    else:
        r[0, 0], r[0, 1], r[1, 0], r[1, 1] = c, -s, s, c
    return r


def _apply_arms_down(stage, wrec, rng: random.Random, _worker_idx: int) -> bool:
    """캐릭터 작업자의 스킨드 메시에 팔 내린 자세를 CPU 로 구워 넣는다.

    이 리그(Character Creator — `RL_BoneRoot/...`)는 관절이 prim 으로 존재하지
    않고 `skel:joints` 목록 + `skel:bindTransforms` 행렬로만 저장된다.
    SkelAnimation 을 skel:animationSource 로 거는 길은 렌더러 평가 타이밍에
    좌우돼 간헐적으로 바인드 자세(T자)로 되돌아갔다 — 대신 정점을 직접
    변형해 결과를 확정한다.

    주의 — 이 에셋의 bindTransforms 는 **전치 저장**(변환이 행3에 있음)되어
    있어 로드 직후 전치해 열 벡터 규약으로 통일한다. 로컬 휴지 변환은
    `inv(부모바인드) @ 자식바인드` 로 복원한다. 팔을 내리는 축은 추측하지
    않는다: 각 축·부호로 돌려 보고 손 말단의 스켈레톤공간 z 를 가장 낮추는
    조합을 채택한다 (렌더 없이 순수 체인 수학).

    반환: 베이크 성공 여부.
    """
    if wrec.get("asset") != "character":
        return False
    import numpy as np
    from pxr import UsdSkel

    base = wrec["path"] + "/char"
    skel_prim = None
    for prim in stage.Traverse():
        if str(prim.GetPath()).startswith(base) and prim.GetTypeName() == "Skeleton":
            skel_prim = prim
            break
    if skel_prim is None:
        return False

    skel = UsdSkel.Skeleton(skel_prim)
    joints = [str(x) for x in skel.GetJointsAttr().Get()]
    # 전치 저장된 행렬 → 열 벡터 규약으로 통일
    bind = [np.array(m, dtype=float).T for m in (skel.GetBindTransformsAttr().Get() or [])]
    if not joints or len(bind) != len(joints):
        return False

    parent = {j: (j.rsplit("/", 1)[0] if "/" in j else None) for j in joints}
    jidx = {j: i for i, j in enumerate(joints)}
    locals_rest = []
    for j in joints:
        pp = parent[j]
        pm = bind[jidx[pp]] if pp in jidx else np.eye(4)
        locals_rest.append(np.linalg.inv(pm) @ bind[jidx[j]])

    def world_mat(jname: str, mods: dict):
        m = np.eye(4)
        chain = []
        j = jname
        while j is not None:
            chain.append(j)
            j = parent.get(j)
        for j in reversed(chain):
            m = m @ locals_rest[jidx[j]]
            if j in mods:
                m = m @ mods[j]
        return m

    # 손 말단 = 각 쪽에서 가장 깊은 hand/finger 관절
    leaves = {}
    for side in ("L_", "R_"):
        arm_j = [j for j in joints if side in j and ("hand" in j.lower() or "finger" in j.lower())]
        leaves[side] = max(arm_j, key=len) if arm_j else None
    if not all(leaves.values()):
        return False

    mods = {}
    for side in ("L_", "R_"):
        leaf = leaves[side]
        upper = f"{side.lower()}upperarm"
        for jname in [j for j in joints if j.rsplit("/", 1)[-1].lower() == upper]:
            z0 = world_mat(leaf, mods)[2, 3]
            best = (z0, "Z", 1)
            for ax in "XYZ":
                for sgn in (1, -1):
                    mods[jname] = _rot4(ax, 65 * sgn)
                    z1 = world_mat(leaf, mods)[2, 3]
                    if z1 < best[0]:
                        best = (z1, ax, sgn)
            best_deg = 65
            for deg in (45, 55, 65, 75, 85):
                mods[jname] = _rot4(best[1], deg * best[2])
                if world_mat(leaf, mods)[2, 3] < best[0]:
                    best_deg = deg
            # 작업자마다 팔 각도를 살짝 다르게 — 자세 일변도 방지
            deg = max(40.0, min(90.0, best_deg + rng.uniform(-12.0, 12.0)))
            mods[jname] = _rot4(best[1], deg * best[2])

    # 팔꿈치를 살짝 굽혀 자연스럽게 (10~30도, 내리는 축 자동 탐색)
    for jname in [j for j in joints if j.rsplit("/", 1)[-1].lower() in ("l_forearm", "r_forearm")]:
        leaf = leaves["L_" if "/L_" in jname else "R_"]
        z0 = world_mat(leaf, mods)[2, 3]
        best = (z0, None, 1)
        for ax in "XYZ":
            for sgn in (1, -1):
                trial = dict(mods)
                trial[jname] = _rot4(ax, 20 * sgn)
                z1 = world_mat(leaf, trial)[2, 3]
                if z1 < best[0] * 0.999:
                    best = (z1, ax, sgn)
        if best[1]:
            mods[jname] = _rot4(best[1], rng.uniform(10.0, 30.0) * best[2])

    # CPU 스키닝 베이크 — 변형된 정점을 points 에 직접 쓴다. 렌더러의
    # 스키닝 평가는 타이밍·무효화 경합으로 불안정했다(간헐 T자 회귀).
    # 정점을 구우면 결과가 확정적이고 이후 prim 이동과 무관하게 유지된다.
    _bake_char_pose(stage, base, joints, bind, world_mat, mods)
    return True


def _bake_char_pose(stage, char_path, joints, bind, world_mat, mods):
    """자세를 스킨드 메시 정점에 CPU 선형 블렌드 스키닝으로 구워 넣는다.

    스키닝 행렬 skin_j = posedJ_j @ inv(bind_j) 를 각 정점에 가중합으로
    적용한 뒤 inv(geomBindTransform) 을 곱해 로컬 points 로 되돌린다 —
    디포머가 바인드 자세(≈항등)로 재평가해도 구운 자세가 그대로 보인다.
    """
    import numpy as np
    from pxr import Gf, UsdGeom  # noqa: N806

    jidx = {j: i for i, j in enumerate(joints)}
    skin = [world_mat(j, mods) @ np.linalg.inv(bind[jidx[j]]) for j in joints]
    n_mesh = n_vert = 0
    max_d = 0.0
    seen = []
    for prim in stage.Traverse():
        path = str(prim.GetPath())
        if not path.startswith(char_path + "/"):
            continue
        seen.append(f"{prim.GetTypeName()}:{path.rsplit('/', 1)[-1]}")
        mesh = UsdGeom.Mesh(prim)
        if not mesh:
            continue
        pts_attr = mesh.GetPointsAttr()
        pts = pts_attr.Get()
        if pts is None or len(pts) == 0:
            continue
        api = UsdGeom.PrimvarsAPI(prim)
        pv_ji = api.GetPrimvar("skel:jointIndices")
        pv_jw = api.GetPrimvar("skel:jointWeights")
        if not pv_ji or not pv_jw:
            continue
        gb_attr = prim.GetAttribute("skel:geomBindTransform")
        gb = np.array(gb_attr.Get(), dtype=float).T if gb_attr and gb_attr.Get() else np.eye(4)
        # GetIndices() 는 비인덱스 primvar 에서 빈 배열을 돌려준다 —
        # None 이 아니므로 falsy 로 판정해야 Get() 폴백이 동작한다.
        joint_idx = pv_ji.GetIndices() or pv_ji.Get()
        weights = pv_jw.GetIndices() or pv_jw.Get()
        if not joint_idx or not weights:
            continue
        joint_idx, weights = list(joint_idx), list(weights)
        mj = prim.GetAttribute("skel:joints")
        if mj and mj.Get():
            remap = [jidx.get(str(m), 0) for m in mj.Get()]
            joint_idx = [remap[x] for x in joint_idx]
        n = len(pts)
        epv = len(joint_idx) // n if n else 0
        if epv == 0:
            continue
        p_hom = np.hstack([np.array(pts, dtype=float), np.ones((n, 1))])
        pb = (gb @ p_hom.T).T
        d_skel = np.zeros((n, 4))
        for k in range(epv):
            wc = np.array([weights[v * epv + k] for v in range(n)])
            mstack = np.stack([skin[joint_idx[v * epv + k]] for v in range(n)])
            d_skel += wc[:, None] * np.einsum("nij,nj->ni", mstack, pb)
        local = (np.linalg.inv(gb) @ d_skel.T).T
        max_d = max(max_d, float(np.abs(local[:, :3] - np.array(pts)).max()))
        pts_attr.Set([Gf.Vec3f(float(r[0]), float(r[1]), float(r[2])) for r in local])
        n_mesh += 1
        n_vert += n
    print(f"[bake] {char_path}: {n_mesh} meshes, {n_vert} verts, max move {max_d:.3f}m")
    if n_mesh == 0:
        print(f"[bake]   {len(seen)} prims under char: {seen[:20]}")


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
    p.add_argument(
        "--settle",
        type=int,
        default=25,
        help="무작위화 후 렌더 안정화 스텝 — ⚠️ 캐릭터 스킨드 메시는 prim 이동을 "
        "즉시 안 따라온다. 2스텝이면 메시가 이전 자리에 남아 라벨이 빈 공간에 뜬다",
    )
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
    from pxr import Gf, UsdGeom, UsdShade  # noqa: N806

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

    # PPE 색 입히기 — 캐릭터 에셋의 조끼·안전모 메시는 몸 전체와 같은
    # BaseColor 텍스처 아틀라스를 써서 회색 톤으로 나온다. 머티리얼 바인딩
    # 교체는 스킨드 메시 렌더 경로에서 무시되므로 (실측), 에셋 셰이더에
    # diffuse_tint 를 직접 넣어 색을 곱한다 — 텍스처 디테일은 살고 색만 입혀진다.
    from pxr import Sdf

    from sim.people_spawner import HELMET_RGB, VEST_RGB

    for wrec in workers:
        looks = wrec["path"] + "/char/Looks/"
        for prim in stage.Traverse():
            path = str(prim.GetPath())
            if not path.startswith(looks) or prim.GetTypeName() != "Shader":
                continue
            low = path.lower()
            rgb = HELMET_RGB if "hardhat" in low else VEST_RGB if "safetyvest" in low else None
            if rgb is not None:
                UsdShade.Shader(prim).CreateInput("diffuse_tint", Sdf.ValueTypeNames.Color3f).Set(
                    Gf.Vec3f(*rgb)
                )

    # T자 포즈 해소 — 자작 SkelAnimation(관절 로컬 트랜스폼 1프레임)을 각
    # 작업자 스켈레톤의 skel:animationSource 에 연결한다. 관절 prim 이 없는
    # 리그라 관절 회전 op 는 무효이고 애니메이션만 먹는다 (dbg 검증됨).
    # 실패한 작업자는 바인드 자세(T자)로 둔다 — 바꿨다고 주장하지 않는다.
    n_posed = 0
    for i, wrec in enumerate(workers):
        wrec["posed"] = _apply_arms_down(stage, wrec, rng, i)
        n_posed += wrec["posed"]
    print(f"[pose] 팔 내림 적용 — {n_posed}/{len(workers)}명")

    floor_clear = _floor_probe()

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
            # 위치 이동은 안전하다 — 자세는 정점에 베이크돼 스키닝 재평가와
            # 무관하게 유지된다 (SkelAnimation 시절엔 이동 시 T자로 돌아갔다).
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
            rx = ry = ryaw = 0.0
            for _try in range(8):
                if roll < 0.85:
                    ang = rng.uniform(0, 2 * math.pi)
                    dist = rng.uniform(2.5, 8.0) if roll < 0.70 else rng.uniform(1.2, 2.5)
                    fx, fy = _char_world_xy(stage, focus["path"])
                    rx = fx - math.cos(ang) * dist
                    ry = fy - math.sin(ang) * dist
                    rx = min(max(rx, walk[0] + 0.5), walk[1] - 0.5)
                    ry = min(max(ry, walk[2] + 0.5), walk[3] - 0.5)
                    ryaw = math.degrees(math.atan2(fy - ry, fx - rx))
                    ryaw += rng.uniform(-20.0, 20.0)
                else:
                    rx = rng.uniform(walk[0] + 0.5, walk[1] - 0.5)
                    ry = rng.uniform(walk[2] + 0.5, walk[3] - 0.5)
                    ryaw = rng.uniform(0.0, 360.0)
                # 로봇이 랙 안에 들어가면 카메라가 선반을 뚫고 찍는다 — 재시도
                if floor_clear(rx, ry):
                    break
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
            # 카메라가 선반·벽 안에 들어간 프레임은 완전 검정 — 버린다.
            # 평균 밝기만으론 반만 검정인 프레임이 통과하므로 어두운 픽셀
            # 비율도 같이 본다 (유용한 저조도 부정 프레임은 0.55 기준 안에 든다).
            if float(rgb.mean()) < 5.0:
                continue
            dark = float((rgb.mean(axis=2) < 20.0).mean())
            if dark > 0.55:
                continue
            # 센서 도메인 갭 축소 — 시뮬 렌더는 실기보다 지나치게 깨끗하다.
            # 60% 프레임에 가우시안 노이즈, 그 절반에 미세 블러를 입혀 실기
            # 카메라의 ISP 잡음을 흉낸다 (라벨은 기하에서 나오므로 영향 없음).
            if rng.random() < 0.6:
                import numpy as np

                nprng = np.random.default_rng(rng.randrange(1 << 30))
                sigma = rng.uniform(1.0, 4.5)
                rgb = np.clip(
                    rgb.astype(np.float32) + nprng.normal(0.0, sigma, rgb.shape),
                    0.0,
                    255.0,
                ).astype(np.uint8)
                if rng.random() < 0.5:
                    rgb = cv2.GaussianBlur(rgb, (3, 3), 0)
            depth = camera.get_depth()
            cam_pos, cam_quat = camera.get_world_pose("world")
            rot_inv = _quat_to_mat(cam_quat).T
            k = camera.get_intrinsics_matrix()
            if made == 0:
                # 투영 검증 — 각 작업자의 몸통 중심이 어느 픽셀로 나가는지 출력해
                # preview 이미지의 실제 사람 위치와 대조한다.
                import numpy as np

                for wrec in workers:
                    cx, cy = _char_world_xy(stage, wrec["path"])
                    c = rot_inv @ (np.array([cx, cy, 1.1]) - cam_pos)
                    u = k[0, 2] - k[0, 0] * c[1] / c[0] if c[0] > 0.03 else -1
                    v = k[1, 2] - k[1, 1] * c[2] / c[0] if c[0] > 0.03 else -1
                    print(
                        f"[dbg] {wrec['path'].rsplit('/', 1)[-1]} root={wrec['pos']} "
                        f"char=({cx:.2f},{cy:.2f}) cam=({c[0]:.2f},{c[1]:.2f},{c[2]:.2f}) "
                        f"px=({u:.0f},{v:.0f})"
                    )
                print(f"[dbg] cam_pos={cam_pos} quat={cam_quat}")

            # ── 라벨 계산 — 위치 기반 부위 박스를 카메라로 투영 ──
            lines: list[str] = []
            boxes_for_preview: list[tuple[tuple, int]] = []
            for wrec in workers:
                combo = wrec["combo"]
                # 루트 위치(wrec["pos"])가 아니라 보이는 몸통의 실측 월드 위치
                wx, wy = _char_world_xy(stage, wrec["path"])
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
