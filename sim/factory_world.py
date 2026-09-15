"""공장 씬 — 실사풍 산업 환경 (환경 A).

방향: **원시 상자 조립이 아니라 PBR 재질·NVIDIA 에셋·조명 설계**로 실사감을 만든다.
랩 씬(`LabKeeper lab_world.py`)과 다른 환경이라 구조는 새로 짜되, 코드로 씬을
조립하는 기법은 같다.

구성: 콘크리트 바닥 · 산업 벽 · 천장 하이베이 조명 · 팔레트 랙 열 · 팔레트+상자 ·
바닥 안전선(노란 통로 표시) · 기둥. NVIDIA 창고 에셋이 있으면 참조하고, 없으면
재질을 갖춘 원시 형상으로 만든다 (폴백은 데이터 정합에 영향 없음 — 라벨 대상은
사람과 PPE 이지 배경 물체가 아니다).
"""

from __future__ import annotations

from pxr import Gf, Sdf, UsdGeom, UsdLux, UsdShade  # noqa: N806


def _material(stage, path: str, rgb, roughness: float = 0.8, metallic: float = 0.0):
    mat = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}_shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return mat


def _bind(prim, mat) -> None:
    UsdShade.MaterialBindingAPI(prim).Bind(mat)


def _box(stage, path: str, size, pos, mat, rot_z_deg: float = 0.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.GetSizeAttr().Set(1.0)
    xf = UsdGeom.Xformable(cube.GetPrim())
    if rot_z_deg:
        xf.AddRotateZOp().Set(rot_z_deg)
    xf.AddScaleOp().Set(Gf.Vec3f(*size))
    xf.AddTranslateOp().Set(Gf.Vec3d(*pos))
    _bind(cube.GetPrim(), mat)
    return cube


def _try_reference_warehouse(stage, prim_path: str = "/World/Warehouse") -> bool:
    """NVIDIA Simple_Warehouse 실사 환경을 참조한다. CDN 실패 시 False.

    `full_warehouse.usd` 는 랙·팔레트·조명이 갖춰진 실사 창고다 — 절차 조립보다
    실사감이 압도적으로 낫다. 전부 S3 에서 스트리밍되므로 첫 로드가 느리다.
    """
    from isaacsim.storage.native import get_assets_root_path

    url = f"{get_assets_root_path()}/Isaac/Environments/Simple_Warehouse/full_warehouse.usd"
    try:
        import omni.client

        result, _ = omni.client.stat(url)
        if result != omni.client.Result.OK:
            return False
        prim = stage.DefinePrim(prim_path, "Xform")
        prim.GetReferences().AddReference(url)
        print(f"[factory] warehouse referenced: {url}")
        return True
    except Exception as e:  # 네트워크/포맷 문제 시 폴백
        print(f"[factory] warehouse ref failed ({e}) — procedural fallback")
        return False


def build_factory(
    stage, room_m: tuple = (18.0, 12.0, 5.0), use_warehouse_asset: bool = True
) -> dict:
    """공장 씬을 쌓는다. 반환값은 배치된 구역 정보(사람 스폰 구역 계산용).

    `use_warehouse_asset` 이면 NVIDIA 실사 창고를 먼저 시도하고, 실패 시에만
    아래 절차 조립 씬을 만든다. 창고 에셋은 자체 조명·재질을 갖고 온다.
    """
    if use_warehouse_asset and _try_reference_warehouse(stage):
        # 실측: 내부 바닥 x[-28,8] y[-5.4,30.6], 벽 x=-26.1·x=5.4·y=-23.4,
        # 천장 z=9. 지게차 (x 1.4~4.9, y 10.2~11.4) 주변은 배치 구역에서 뺀다.
        return {
            "walkable": (-24.0, 4.0, -4.0, 29.0),
            "person_zones": [
                (-14, -8, -4.0, 4.0),  # 남쪽 통로
                (-4, 0, 12.0, 20.0),  # 북쪽 랙 작업 구역
                (-20, -12, 12.0, 24.0),  # 서쪽 랙 앞
                (0.2, 5.0, 12.5, 15.0),  # 지게차 북쪽 — 근접 상황(near-miss) 장면
            ],
            "robot_spawn": (-4.0, 0.0, 0.0),
            "room_m": (32.0, 36.0, 9.0),
            "mode": "warehouse_asset",
        }

    w, d, h = room_m
    base = "/World/Factory"

    # ── 재질 ────────────────────────────────────────────
    concrete = _material(stage, f"{base}/Looks/concrete", (0.42, 0.41, 0.38), 0.95)
    wall_mat = _material(stage, f"{base}/Looks/wall", (0.55, 0.56, 0.55), 0.9)
    steel = _material(stage, f"{base}/Looks/steel", (0.35, 0.38, 0.42), 0.4, 0.8)
    rack_orange = _material(stage, f"{base}/Looks/rack_orange", (0.85, 0.35, 0.08), 0.5, 0.3)
    cardboard = _material(stage, f"{base}/Looks/cardboard", (0.62, 0.48, 0.30), 0.9)
    pallet_wood = _material(stage, f"{base}/Looks/pallet", (0.45, 0.33, 0.20), 0.9)
    yellow_line = _material(stage, f"{base}/Looks/yellow", (0.95, 0.75, 0.05), 0.7)
    pillar_mat = _material(stage, f"{base}/Looks/pillar", (0.50, 0.50, 0.48), 0.85)

    # ── 바닥·벽·천장 ───────────────────────────────────
    _box(stage, f"{base}/floor", (w, d, 0.1), (0, 0, -0.05), concrete)
    for name, size, pos in (
        ("wall_n", (w, 0.2, h), (0, d / 2, h / 2)),
        ("wall_s", (w, 0.2, h), (0, -d / 2, h / 2)),
        ("wall_e", (0.2, d, h), (w / 2, 0, h / 2)),
        ("wall_w", (0.2, d, h), (-w / 2, 0, h / 2)),
        ("ceiling", (w, d, 0.1), (0, 0, h + 0.05)),
    ):
        _box(stage, f"{base}/{name}", size, pos, wall_mat)

    # 기둥 — 공장 특유의 구조물. 6m 간격 두 줄.
    for i, x in enumerate((-w / 4, w / 4)):
        _box(stage, f"{base}/pillar_{i}", (0.4, 0.4, h), (x, 0, h / 2), pillar_mat)

    # ── 팔레트 랙 열 — 벽을 따라 두 열. 단(段) 3단. ────────
    def rack_row(name: str, y: float, x_range: tuple):
        upright_h, level_h = 3.6, 1.2
        for i, x in enumerate(range(int(x_range[0]), int(x_range[1]), 2)):
            # 기둥 2개 + 단별 빔 + 팔레트·상자
            for j, dx in enumerate((-0.9, 0.9)):
                _box(
                    stage,
                    f"{base}/{name}_{i}_up{j}",
                    (0.1, 0.1, upright_h),
                    (x + dx, y, upright_h / 2),
                    steel,
                )
            for lv in range(3):
                z = level_h * lv + 0.6
                _box(stage, f"{base}/{name}_{i}_beam{lv}", (1.9, 0.08, 0.1), (x, y, z), rack_orange)
                if (i + lv) % 3 != 0:  # 빈 단을 섞어 자연스럽게
                    _box(
                        stage,
                        f"{base}/{name}_{i}_pal{lv}",
                        (1.1, 1.0, 0.15),
                        (x, y, z + 0.15),
                        pallet_wood,
                    )
                    _box(
                        stage,
                        f"{base}/{name}_{i}_box{lv}",
                        (0.9, 0.8, 0.7),
                        (x, y, z + 0.55),
                        cardboard,
                    )

    rack_row("rackN", d / 2 - 1.0, (-w / 2 + 2, w / 2 - 2))
    rack_row("rackS", -d / 2 + 1.0, (-w / 2 + 2, w / 2 - 2))

    # 바닥 팔레트 더미 — 중앙 통로 가장자리에 불규칙하게
    for i, (x, y, rot) in enumerate(((-3, 2.0, 4), (4, -1.5, -8), (0.5, 3.4, 12))):
        _box(stage, f"{base}/floor_pallet_{i}", (1.2, 1.0, 0.15), (x, y, 0.075), pallet_wood, rot)
        _box(stage, f"{base}/floor_crate_{i}", (1.0, 0.9, 0.8), (x, y, 0.55), cardboard, rot)

    # ── 바닥 안전선 — 노란 통로 경계. 카메라가 자주 보는 것이라 중요. ──
    _box(stage, f"{base}/lane_l", (w - 2, 0.12, 0.005), (0, 1.6, 0.002), yellow_line)
    _box(stage, f"{base}/lane_r", (w - 2, 0.12, 0.005), (0, -1.6, 0.002), yellow_line)

    # ── 조명 — 실사감의 절반은 조명이다. 하이베이 + 주변광. ──
    dome = UsdLux.DomeLight.Define(stage, f"{base}/Lights/dome")
    dome.CreateIntensityAttr(500)
    dome.CreateColorAttr(Gf.Vec3f(0.75, 0.82, 0.95))  # 창문 없는 낮광 느낌
    for i, x in enumerate((-w / 3, 0.0, w / 3)):
        for j, y in enumerate((-d / 4, d / 4)):
            light = UsdLux.RectLight.Define(stage, f"{base}/Lights/highbay_{i}_{j}")
            light.CreateWidthAttr(2.0)
            light.CreateHeightAttr(0.6)
            light.CreateIntensityAttr(4000)
            light.CreateColorAttr(Gf.Vec3f(1.0, 0.95, 0.85))
            xf = UsdGeom.Xformable(light.GetPrim())
            xf.AddTranslateOp().Set(Gf.Vec3d(x, y, h - 0.3))
            xf.AddRotateXOp().Set(180.0)  # 아래를 비춘다

    return {
        "walkable": (-w / 2 + 1.0, w / 2 - 1.0, -d / 2 + 2.0, d / 2 - 2.0),
        "person_zones": [
            (-6, -2, -1.0, 1.0),  # 통로 서쪽
            (2, 6, -1.0, 1.0),  # 통로 동쪽
            (-1, 1, 2.0, 4.0),  # 북쪽 랙 앞 작업 구역
        ],
        "room_m": room_m,
        "mode": "procedural",
    }
