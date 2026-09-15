"""에셋 루트와 창고 환경 USD 가용성 확인."""

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True})

import omni.client  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.storage.native import get_assets_root_path  # noqa: E402

root = get_assets_root_path()
print(f"[assets] root: {root}")

candidates = [
    f"{root}/Isaac/Environments/Simple_Warehouse/warehouse.usd",
    f"{root}/Isaac/Environments/Simple_Warehouse/warehouse_no_forklift.usd",
    f"{root}/Isaac/Environments/Simple_Warehouse/full_warehouse.usd",
    f"{root}/Isaac/Environments/Grid_Room/grid_room.usd",
    f"{root}/Isaac/Environments/Office/office.usd",
]
for url in candidates:
    result, _ = omni.client.stat(url)
    print(f"[assets] {'OK ' if result == omni.client.Result.OK else 'MISS'} {url}")

# 사람 에셋 후보도 같이
for url in (
    f"{root}/Isaac/People/Characters/original_male_adult_construction_05/male_adult_construction_05.usd",
    f"{root}/Isaac/People/Characters",
):
    result, _ = omni.client.stat(url)
    print(f"[assets] {'OK ' if result == omni.client.Result.OK else 'MISS'} {url}")

app.close()
