"""자작 SkelAnimation으로 팔 내리기 — 나머지 자세 + 팔 관절만 회전한 1프레임 포즈."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import numpy as np
from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": 640, "height": 480})

import cv2  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from pxr import Gf, Sdf, Usd, UsdGeom, UsdSkel, Vt  # noqa: E402

from sim.factory_world import build_factory  # noqa: E402
from sim.mechdog_proxy import attach_camera, build_robot  # noqa: E402
from sim.people_spawner import spawn_workers  # noqa: E402

world = World(stage_units_in_meters=1.0)
stage = world.stage
zones = build_factory(stage)
robot_path = build_robot(stage)
workers = spawn_workers(stage, zones["person_zones"], count=6, seed=3, all_parts=True)
camera = attach_camera(robot_path)
world.reset()
camera.initialize()

import random as _rnd
_r = _rnd.Random(7)

def arms_down(w, idx):
    base = w["path"] + "/char"
    skel_prim = None
    for prim in stage.Traverse():
        if str(prim.GetPath()).startswith(base) and prim.GetTypeName() == "Skeleton":
            skel_prim = prim
            break
    if skel_prim is None:
        print(f"[dbg] w{idx} no skeleton")
        return None, None
    skel = UsdSkel.Skeleton(skel_prim)
    joints = [str(x) for x in skel.GetJointsAttr().Get()]
    bind = [np.array(m, dtype=float).T for m in (skel.GetBindTransformsAttr().Get() or [])]
    if not joints or len(bind) != len(joints):
        print(f"[dbg] w{idx} joints={len(joints)} bind={len(bind)}")
        return None, None
    parent = {j: (j.rsplit("/", 1)[0] if "/" in j else None) for j in joints}
    jidx = {j: i for i, j in enumerate(joints)}
    locals_rest = []
    for j in joints:
        pp = parent[j]
        pm = bind[jidx[pp]] if pp in jidx else np.eye(4)
        locals_rest.append(np.linalg.inv(pm) @ bind[jidx[j]])

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
    if not all(leaves.values()):
        print(f"[dbg] w{idx} no leaves")
        return None, None
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
            best_deg = 65
            for deg in (45, 55, 65, 75, 85):
                mods[jname] = rot4(best[1], deg * best[2])
                if world_mat(leaf, mods)[2, 3] < best[0]:
                    best_deg = deg
            mods[jname] = rot4(best[1], best_deg * best[2])
            print(f"[dbg] w{idx} {jname.rsplit('/',1)[-1]}: rot{best[1]}{best_deg*best[2]:+.0f} 손z {z0:.2f}->{best[0]:.2f}")

    rotations, translations, scales = [], [], []
    for i, j in enumerate(joints):
        m = locals_rest[i] @ mods.get(j, np.eye(4))
        gm = Gf.Matrix4d(*m.T.flatten().tolist())
        q = gm.ExtractRotationQuat()
        rotations.append(Gf.Quatf(float(q.GetReal()), Gf.Vec3f(*q.GetImaginary())))
        t = gm.ExtractTranslation()
        translations.append(Gf.Vec3f(float(t[0]), float(t[1]), float(t[2])))
        scales.append(Gf.Vec3h(1.0, 1.0, 1.0))
    anim = stage.DefinePrim(f"/World/Anims/pose_w{idx}", "SkelAnimation")
    anim.CreateAttribute("joints", Sdf.ValueTypeNames.TokenArray).Set(Vt.TokenArray(joints))
    anim.CreateAttribute("rotations", Sdf.ValueTypeNames.QuatfArray).Set(
        Vt.QuatfArray(rotations), Usd.TimeCode(0)
    )
    anim.CreateAttribute("translations", Sdf.ValueTypeNames.Float3Array).Set(
        Vt.Vec3fArray(translations), Usd.TimeCode(0)
    )
    anim.CreateAttribute("scales", Sdf.ValueTypeNames.Half3Array).Set(
        Vt.Vec3hArray(scales), Usd.TimeCode(0)
    )
    rel = skel_prim.GetRelationship("skel:animationSource") or skel_prim.CreateRelationship(
        "skel:animationSource"
    )
    rel.SetTargets([anim.GetPath()])
    return skel_prim, anim


anims = []
for i, w in enumerate(workers):
    anims.append(arms_down(w, i))
print(f"[dbg] applied {sum(1 for a in anims if a[0] is not None)}/{len(workers)}")

out = Path("datasets/ppe_test/diag")
out.mkdir(parents=True, exist_ok=True)

# 첫 작업자를 로봇 앞에
w = workers[0]
base = w["path"] + "/char"
rel = anims[0][0].GetRelationship("skel:animationSource") if anims[0][0] else None
print(f"[dbg] animationSource w0 -> {rel.GetTargets() if rel else None}")

# 배치 + 렌더 — 작업자를 로봇 정면 3m에
api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(w["path"]))
api.SetTranslate(Gf.Vec3d(0.0, 0.0, 0.0))
xf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
xf.AddTranslateOp().Set(Gf.Vec3d(0.0, -5.5, 0.0))
xf.AddRotateZOp().Set(90.0)
# gen과 같은 조건 재현 — 매 25스텝 작업자 이동 + 회전 + PPE 토글
wprim = stage.GetPrimAtPath(w["path"])
ppe = []
for prim in stage.Traverse():
    if str(prim.GetPath()).startswith(base) and "hardhat" in str(prim.GetPath()).lower():
        ppe.append(prim)
print(f"[dbg] ppe prims={len(ppe)}")
import random
rng2 = random.Random(99)
rxf = UsdGeom.Xformable(stage.GetPrimAtPath(robot_path))
ro = rxf.GetOrderedXformOps()
all_ppe = []
for w in workers:
    bb = w["path"] + "/char"
    for prim in stage.Traverse():
        nm = str(prim.GetPath()).lower()
        if nm.startswith(bb) and ("hardhat" in nm or "safetyvest" in nm):
            all_ppe.append(prim)
print(f"[dbg] all_ppe={len(all_ppe)}")
frame = 0
rels = []
for i, w in enumerate(workers):
    if anims[i][0] is not None and anims[i][1] is not None:
        rels.append((anims[i][0].GetRelationship("skel:animationSource"), anims[i][1].GetPath()))
for i in range(300):
    if i % 25 == 0:
        for w in workers:
            zone = zones["person_zones"][rng2.randrange(len(zones["person_zones"]))]
            a2 = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(w["path"]))
            a2.SetTranslate(Gf.Vec3d(rng2.uniform(zone[0], zone[1]), rng2.uniform(zone[2], zone[3]), 0.0))
            a2.SetRotate((0.0, 0.0, rng2.uniform(0, 360)), UsdGeom.XformCommonAPI.RotationOrderZYX)
        for prim in all_ppe:
            img = UsdGeom.Imageable(prim)
            img.MakeVisible() if rng2.random() < 0.5 else img.MakeInvisible()
        # 텔레포트가 스키닝을 바인드 자세로 되돌린다 — 관계를 끊었다 다시
        # 연결해 강제 재평가를 시도한다.
        for r, ap in rels:
            r.SetTargets([])
            r.SetTargets([ap])
        frame += 1
    world.step(render=True)
    # 각 캡처 시점에 서로 다른 작업자를 바라봄 — 전원 자세 확인용
    if i in (30, 100, 150, 200, 250, 299):
        tgt = workers[min(i // 50, len(workers) - 1)]
        tp = stage.GetPrimAtPath(tgt["path"])
        tm = UsdGeom.Xformable(tp).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        tx, ty = float(tm.ExtractTranslation()[0]), float(tm.ExtractTranslation()[1])
        for op in ro:
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(tx, ty - 3.5, 0.0))
            elif op.GetOpType() == UsdGeom.XformOp.TypeRotateZ:
                op.Set(90.0)
        world.step(render=True)
        rgba = camera.get_rgba()
        if rgba is not None and rgba.size:
            cv2.imwrite(str(out / f"posed_{i:03d}.jpg"), cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2BGR))
            print(f"[dbg] saved posed_{i:03d}.jpg target={tgt['path']}")

# 메시 붕괴 여부 — 캐릭터 월드 바운딩박스 크기
cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
bb = cache.ComputeWorldBound(stage.GetPrimAtPath(base)).ComputeAlignedBox()
print(f"[dbg] char bbox size={bb.GetMax() - bb.GetMin()} min={bb.GetMin()}")

# 비교: 애니메이션 제거 후 바인드 상태 렌더
if rel and anims[0][1]:
    rel.RemoveTarget(anims[0][1].GetPath())
for _ in range(10):
    world.step(render=True)
rgba0 = camera.get_rgba()
if rgba0 is not None and rgba0.size:
    cv2.imwrite(str(out / "tpose.jpg"), cv2.cvtColor(rgba0[..., :3], cv2.COLOR_RGB2BGR))
    print("[dbg] saved tpose.jpg")
app.close()
print("[dbg] done")
