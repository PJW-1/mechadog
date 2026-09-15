"""사람 스폰 — PPE 착용 4조합의 근로자 배치.

실사감 목표: `omni.anim.people` / Replicator 캐릭터 USD 가 잡히면 그걸 쓰고,
못 잡으면 **치수가 맞는 합성 인형**(캡슐 몸통 + 구 머리 + 안전모 돔 + 조끼
껍데기)으로 세운다. ⚠️ 폴백 인형은 *실사가 아니다* — 그래도 안전모·조끼의
색·위치·크기 비율은 맞으므로 **2단 검출기 학습의 근사 데이터**가 된다.
실사급은 캐릭터 에셋이 잡힐 때까지 대기다.

착용 조합은 라벨 정답지다 — 4가지를 골고루 섞는다:
  helmet+vest / helmet only / vest only / neither
"""

from __future__ import annotations

import random

from pxr import Gf, UsdGeom  # noqa: N806

#: 착용 조합 — PPE 라벨의 4가지 정답 케이스
WEAR_COMBOS = (
    ("helmet", "vest"),
    ("helmet",),
    ("vest",),
    (),
)

#: PPE 색 — 실물 주황 안전모 · 연두 형광 조끼 (BGR 아니라 RGB)
HELMET_RGB = (1.0, 0.42, 0.05)
VEST_RGB = (0.55, 0.95, 0.15)
SKIN_RGB = (0.85, 0.65, 0.55)
WORKWEAR_RGB = (0.25, 0.30, 0.35)  # 작업복 남청색


def _try_spawn_character(stage, prim_path: str, prefer: int = 0) -> bool:
    """Isaac 사람 에셋을 찾아본다. 있으면 True."""
    try:
        from isaacsim.storage.native import get_assets_root_path

        root = get_assets_root_path()
        if not root:
            return False
        candidates = [
            "/Isaac/People/Characters/original_male_adult_construction_05/male_adult_construction_05.usd",
            "/Isaac/People/Characters/male_adult_construction_05_new/male_adult_construction_05_new.usd",
        ]
        # 작업자마다 다른 에셋부터 시도 — 전원 동일 인물이면 학습 다양성이 없다
        candidates = candidates[prefer:] + candidates[:prefer]
        from isaacsim.core.utils.stage import add_reference_to_stage

        for cand in candidates:
            try:
                add_reference_to_stage(usd_path=root + cand, prim_path=prim_path)
                if stage.GetPrimAtPath(prim_path).IsValid():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _spawn_fallback_humanoid(stage, prim_path: str, wears: tuple, mat_cache: dict) -> None:
    """치수가 맞는 합성 인형 — 170cm 성인 비율."""
    from .factory_world import _bind, _material

    def mat(key, rgb):
        if key not in mat_cache:
            mat_cache[key] = _material(stage, f"/World/Factory/Looks/{key}", rgb)
        return mat_cache[key]

    # 몸통 — 작업복
    torso = UsdGeom.Capsule.Define(stage, f"{prim_path}/torso")
    torso.GetRadiusAttr().Set(0.16)
    torso.GetHeightAttr().Set(0.55)
    UsdGeom.Xformable(torso.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(0, 0, 1.05))
    _bind(torso.GetPrim(), mat("workwear", WORKWEAR_RGB))
    # 다리
    for side in (-1, 1):
        leg = UsdGeom.Capsule.Define(stage, f"{prim_path}/leg_{side:+d}")
        leg.GetRadiusAttr().Set(0.07)
        leg.GetHeightAttr().Set(0.75)
        UsdGeom.Xformable(leg.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(0, side * 0.09, 0.42))
        _bind(leg.GetPrim(), mat("workwear_dark", (0.18, 0.20, 0.25)))
    # 머리
    head = UsdGeom.Sphere.Define(stage, f"{prim_path}/head")
    head.GetRadiusAttr().Set(0.11)
    UsdGeom.Xformable(head.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(0, 0, 1.62))
    _bind(head.GetPrim(), mat("skin", SKIN_RGB))
    # 안전모 — 머리 위의 반구 돔 (착용 시만)
    if "helmet" in wears:
        dome = UsdGeom.Sphere.Define(stage, f"{prim_path}/helmet")
        dome.GetRadiusAttr().Set(0.135)
        xf = UsdGeom.Xformable(dome.GetPrim())
        xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 1.66))
        xf.AddScaleOp().Set(Gf.Vec3f(1.0, 1.0, 0.7))
        _bind(dome.GetPrim(), mat("helmet_orange", HELMET_RGB))
    # 조끼 — 몸통을 감싸는 얇은 껍데기 (착용 시만)
    if "vest" in wears:
        vest = UsdGeom.Capsule.Define(stage, f"{prim_path}/vest")
        vest.GetRadiusAttr().Set(0.185)
        vest.GetHeightAttr().Set(0.45)
        xf = UsdGeom.Xformable(vest.GetPrim())
        xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, 1.12))
        xf.AddScaleOp().Set(Gf.Vec3f(1.0, 0.9, 1.0))
        _bind(vest.GetPrim(), mat("vest_lime", VEST_RGB))


def spawn_workers(
    stage, zones: list, count: int = 6, seed: int = 7, all_parts: bool = False
) -> list[dict]:
    """구역 안에 근로자를 놓는다. 배치 정보(조합·위치)를 돌려준다.

    `all_parts` 이면 모두에게 안전모·조끼 prim 을 달아 둔다 — 데이터셋
    생성기가 프레임마다 가시성을 토글해 4조합을 만들 수 있게.
    """
    rng = random.Random(seed)
    mat_cache: dict = {}
    spawned = []
    for i in range(count):
        zone = zones[i % len(zones)]
        x = rng.uniform(zone[0], zone[1])
        y = rng.uniform(zone[2], zone[3])
        yaw = rng.uniform(0, 360)
        wears = WEAR_COMBOS[i % len(WEAR_COMBOS)]
        if all_parts:
            wears = ("helmet", "vest")
        # ⚠️ 캐릭터 에셋의 자체 xform 은 애니메이션·리그가 매 스텝 덮어써서
        # 우리가 쓴 translate 가 지워진다 (실측: SetTranslate 후에도 (0,0,0)).
        # 그래서 부모 Xform 을 만들고 캐릭터는 그 안에 둔다 — 자식이 무엇을
        # 쓰든 부모의 위치·회전은 합성으로 살아남는다.
        path = f"/World/Workers/worker_{i}"
        inner = f"{path}/char"
        stage.DefinePrim(path, "Xform")
        asset = "character" if _try_spawn_character(stage, inner, prefer=i) else "fallback"
        if asset == "fallback":
            _spawn_fallback_humanoid(stage, inner, wears, mat_cache)
        api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath(path))
        api.SetTranslate(Gf.Vec3d(x, y, 0))
        api.SetRotate((0.0, 0.0, yaw), UsdGeom.XformCommonAPI.RotationOrderZYX)
        spawned.append({"path": path, "wears": wears, "pos": (x, y), "yaw": yaw, "asset": asset})
    return spawned
