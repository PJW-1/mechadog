"""스킨드 메시 CPU 베이크 검증 — 팔 내린 정점을 points 에 직접 쓰기."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np
from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": 640, "height": 480})

import cv2  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from pxr import Gf, Usd, UsdGeom, UsdSkel  # noqa: E402

from sim.factory_world import build_factory  # noqa: E402
from sim.mechdog_proxy import attach_camera, build_robot  # noqa: E402
from sim.people_spawner import spawn_workers  # noqa: E402

world = World(stage_units_in_meters=1.0)
stage = world.stage
zones = build_factory(stage)
robot_path = build_robot(stage)
workers = spawn_workers(stage, zones["person_zones"], count=2, seed=3, all_parts=True)
camera = attach_camera(robot_path)
world.reset()
camera.initialize()

w = workers[0]
base = w["path"] + "/char"

# 스켈레톤 탐색 + 관절·바인드 읽기 (dbg_anim 과 동일)
skel_prim = None
for prim in stage.Traverse():
    if str(prim.GetPath()).startswith(base) and prim.GetTypeName() == "Skeleton":
        skel_prim = prim
        break
skel = UsdSkel.Skeleton(skel_prim)
joints = [str(x) for x in skel.GetJointsAttr().Get()]
bind = [np.array(m, dtype=float).T for m in skel.GetBindTransformsAttr().Get()]
parent = {j: (j.rsplit("/", 1)[0] if "/" in j else None) for j in joints}
jidx = {j: i for i, j in enumerate(joints)}
locals_rest = []
for j in joints:
    pp = parent[j]
    pm = bind[jidx[pp]] if pp in jidx else np.eye(4)
    locals_rest.append(np.linalg.inv(pm) @ bind[jidx[j]])


def rot4(axis, deg):
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


def world_mat(jname, mods):
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


leaves = {}
for side in ("L_", "R_"):
    arm_j = [j for j in joints if side in j and ("hand" in j.lower() or "finger" in j.lower())]
    leaves[side] = max(arm_j, key=len) if arm_j else None

# 어깨~손 체인의 모든 관절명 + 부모 — twist/share 보조 뼈 확인용
for j in joints:
    seg = j.rsplit("/", 1)[-1].lower()
    if any(k in seg for k in ("clavicle", "arm", "hand", "twist", "share", "elbow")):
        print(f"[bones] {j}  parent={parent[j]}")

mods = {}
for side in ("L_", "R_"):
    leaf = leaves[side]
    upper = f"{side.lower()}upperarm"
    for jname in [j for j in joints if j.rsplit("/", 1)[-1].lower() == upper]:
        z0 = world_mat(leaf, mods)[2, 3]
        best = (z0, "Z", 1)
        for ax in "XYZ":
            for sgn in (1, -1):
                mods[jname] = rot4(ax, 65 * sgn)
                z1 = world_mat(leaf, mods)[2, 3]
                if z1 < best[0]:
                    best = (z1, ax, sgn)
        mods[jname] = rot4(best[1], 75 * best[2])
        zend = world_mat(leaf, mods)[2, 3]
        print(
            f"[sel] {jname.rsplit('/',1)[-1]} leaf={leaf.rsplit('/',1)[-1]} "
            f"axis={best[1]} sgn={best[2]} handz {z0:.2f}->{zend:.2f}"
        )
for jname in [j for j in joints if j.rsplit("/", 1)[-1].lower() in ("l_forearm", "r_forearm")]:
    leaf = leaves["L_" if "/L_" in jname else "R_"]
    z0 = world_mat(leaf, mods)[2, 3]
    best = (z0, None, 1)
    for ax in "XYZ":
        for sgn in (1, -1):
            trial = dict(mods)
            trial[jname] = rot4(ax, 20 * sgn)
            z1 = world_mat(leaf, trial)[2, 3]
            if z1 < best[0] * 0.999:
                best = (z1, ax, sgn)
    if best[1]:
        mods[jname] = rot4(best[1], 20.0 * best[2])

# 관절 월드(스켈 공간) + 스키닝 행렬
skelJ = {j: world_mat(j, mods) for j in joints}
skin = [skelJ[j] @ np.linalg.inv(bind[jidx[j]]) for j in joints]

# char 월드 변환
char_prim = stage.GetPrimAtPath(base)
C_char = np.array(
    UsdGeom.Xformable(char_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()),
    dtype=float,
).T

# char 아래 스킨드 메시 전부 베이크
n_baked = 0
for prim in stage.Traverse():
    path = str(prim.GetPath())
    if not path.startswith(base + "/"):
        continue
    mesh = UsdGeom.Mesh(prim)
    if not mesh:
        continue
    pts_attr = mesh.GetPointsAttr()
    pts = pts_attr.Get()
    if pts is None or len(pts) == 0:
        continue
    # 스키닝 primvar 확인
    pv_ji = UsdGeom.PrimvarsAPI(prim).GetPrimvar("skel:jointIndices")
    pv_jw = UsdGeom.PrimvarsAPI(prim).GetPrimvar("skel:jointWeights")
    if not pv_ji or not pv_jw:
        continue
    method_attr = prim.GetAttribute("skel:skinningMethod")
    method = method_attr.Get() if method_attr else None
    gb_attr = prim.GetAttribute("skel:geomBindTransform")
    gb = (
        np.array(gb_attr.Get(), dtype=float).T
        if gb_attr and gb_attr.Get()
        else np.eye(4)
    )
    ji_idx = pv_ji.GetIndices()
    ji_base = pv_ji.Get()
    jw_idx = pv_jw.GetIndices()
    jw_base = pv_jw.Get()
    if ji_idx:
        joint_idx = list(ji_idx)
    elif ji_base is not None:
        joint_idx = list(ji_base)
    else:
        continue
    if jw_idx:
        weights = list(jw_idx)
    elif jw_base is not None:
        weights = list(jw_base)
    else:
        continue
    # 메시 자체 skel:joints 서브셋 매핑
    mj_attr = prim.GetAttribute("skel:joints")
    mjoints = [str(x) for x in mj_attr.Get()] if mj_attr and mj_attr.Get() else None
    if mjoints:
        remap = [jidx[m] if m in jidx else 0 for m in mjoints]
        joint_idx = [remap[x] for x in joint_idx]
    epv = len(joint_idx) // len(pts) if len(pts) else 0
    if epv == 0:
        continue
    print(
        f"[bake] {path} verts={len(pts)} epv={epv} method={method} "
        f"ji_indexed={bool(ji_idx)}"
    )
    P = np.array([[p[0], p[1], p[2], 1.0] for p in pts], dtype=float)  # (N,4)
    Pb = (gb @ P.T).T  # 바인드 공간
    D = np.zeros((len(pts), 4))
    for k in range(epv):
        wcol = np.array([weights[v * epv + k] for v in range(len(pts))])
        jcol = [joint_idx[v * epv + k] for v in range(len(pts))]
        M = np.stack([skin[j] for j in jcol])  # (N,4,4)
        D += wcol[:, None] * np.einsum("nij,nj->ni", M, Pb)
    # UsdSkel 렌더 경로: world = M_skel @ (Σ w·skelJ·invBind) @ G @ p
    # 바인드 평가 시 스키닝 행렬이 ≈항등이라 world ≈ M_skel @ G @ p.
    # 베이크 정점은 p_new = inv(G) @ D_skel 로 쓰면 world = M_skel @ D_skel.
    L = (np.linalg.inv(gb) @ D.T).T
    print(
        f"[bake]   L bbox min={L.min(axis=0)[:3]} max={L.max(axis=0)[:3]} "
        f"| P orig min={P.min(axis=0)[:3]} max={P.max(axis=0)[:3]} "
        f"| D skel min={D.min(axis=0)[:3]} max={D.max(axis=0)[:3]}"
    )
    out = [Gf.Vec3f(float(r[0]), float(r[1]), float(r[2])) for r in L]
    pts_attr.Set(out)
    n_baked += 1

print(f"[bake] baked {n_baked} meshes")

# 스키닝 바인딩은 유지한다 — 디포머가 바인드 자세(≈항등)로 재적용해도
# 베이크된 정점은 그대로 보인다. (끊으면 스킨드 prim 이 렌더되지 않음)

# 렌더 비교 — 작업자 0(베이크) vs 작업자 1(원본 T자)
api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(w["path"]))
api.SetTranslate(Gf.Vec3d(0.0, 0.0, 0.0))
api2 = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(workers[1]["path"]))
api2.SetTranslate(Gf.Vec3d(1.5, 0.0, 0.0))
xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
xf.AddTranslateOp().Set(Gf.Vec3d(0.75, -5.0, 0.0))
xf.AddRotateZOp().Set(90.0)

# gen 조건 재현 — 매 10스텝 작업자 텔레포트 + PPE 가시성 토글
import random as _r2

rng = _r2.Random(5)
ppe_prims = []
for prim in stage.Traverse():
    nm = str(prim.GetPath()).lower()
    if nm.startswith(base.lower() + "/") and ("hardhat" in nm or "safetyvest" in nm):
        ppe_prims.append(prim)
print(f"[bake] ppe prims={len(ppe_prims)}")
for i in range(120):
    if i % 10 == 0:
        api.SetTranslate(Gf.Vec3d(0.0, 0.0, 0.0))
        api.SetRotate((0.0, 0.0, rng.uniform(0, 360)), UsdGeom.XformCommonAPI.RotationOrderZYX)
        for pp in ppe_prims:
            img = UsdGeom.Imageable(pp)
            img.MakeVisible() if rng.random() < 0.5 else img.MakeInvisible()
    world.step(render=True)
rgba = camera.get_rgba()
out = Path("datasets/ppe_test/diag")
out.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(out / "baked.jpg"), cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2BGR))
print("[bake] saved baked.jpg")
app.close()
