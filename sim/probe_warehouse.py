"""창고 USD 의 실제 크기·내부 구조를 잰다 — 배치 좌표 결정용."""

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True})

from isaacsim.core.api import World  # noqa: E402
from pxr import UsdGeom  # noqa: E402

from sim.factory_world import _try_reference_warehouse  # noqa: E402

world = World(stage_units_in_meters=1.0)
stage = world.stage
_try_reference_warehouse(stage)
world.reset()
for _ in range(5):
    world.step(render=True)

cache = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_])
prim = stage.GetPrimAtPath("/World/Warehouse")
bbox = cache.ComputeWorldBound(prim).ComputeAlignedBox()
print(
    f"[wh] bounds min={tuple(round(v, 1) for v in bbox.GetMin())} "
    f"max={tuple(round(v, 1) for v in bbox.GetMax())}"
)

# 최상위 자식 구조 — 랙·벽·바닥이 어떤 prim 인지 본다
for child in prim.GetChildren():
    b = cache.ComputeWorldBound(child).ComputeAlignedBox()
    print(
        f"[wh] {child.GetName():40s} "
        f"min={tuple(round(v, 1) for v in b.GetMin())} "
        f"max={tuple(round(v, 1) for v in b.GetMax())}"
    )

app.close()
