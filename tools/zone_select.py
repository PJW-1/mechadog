"""순찰 구역 좌표화 — 지도를 띄우고 클릭으로 좌표를 붙인다 (FR-7.1 · 2단계).

    python tools/zone_select.py            # maps/ 의 지도를 띄운다
    python tools/zone_select.py --list     # 저장된 구역만 확인한다 (창 없음)

**구역 라벨을 여기서 만들지 않는다.** 정본은 `config.yaml` 의 `zones.ids`
(`[A, B, C]`)이고, 이 도구는 그 라벨에 **좌표를 붙일 뿐이다** (`zones.py` 머리말).
그래서 클릭할 수 있는 횟수가 라벨 수만큼으로 제한된다 — 구역을 늘리려면
설정을 고친다.

조작 — 왼쪽 클릭 두 번: 첫 클릭이 구역 자리, 둘째 클릭이 **점검할 때 바라볼 방향**이다
(그 자리에서 둘째 클릭 지점을 본다) / `u`: 마지막 취소 / `s`: 저장. 창을 닫아도 저장한다.
방향은 변화 감지가 기준과 같은 장면을 보게 하는 값이다(`runtime._aligned`).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.behavior.zones import Zone, ZoneStore, nearest_free_cell
from host.common.config import ConfigError
from host.common.console import survive_encoding_errors
from host.common.logging_setup import event_logger, setup_logging
from host.slam import settings
from host.slam.occupancy import OccupancyGrid
from host.slam.viz import to_image

LOG = event_logger("mechadog.tools.zone_select")

#: 막힌 셀을 클릭했을 때 대체 자리를 찾을 반경. 지도 절반을 훑을 이유는 없다 —
#: 그렇게 멀리 옮겨야 한다면 클릭이 잘못된 것이고 사용자가 다시 찍어야 한다.
SEARCH_RADIUS_CELLS = 40


def place(
    store: ZoneStore, grid: OccupancyGrid, x: float, y: float, free_thresh: float
) -> Zone | None:
    """클릭 지점에 구역을 붙인다. 막혀 있으면 가까운 빈 셀로 옮긴다."""
    row, col = grid.to_cell(x, y)
    if not grid.inside(row, col):
        print("[Zone] 지도 범위 밖이다")
        return None
    cell = nearest_free_cell(
        grid, row, col, free_thresh=free_thresh, max_radius_cells=SEARCH_RADIUS_CELLS
    )
    if cell is None:
        print("[Zone] 주변에 확실히 빈 셀이 없다 — 지도를 더 만들거나 다른 곳을 찍는다")
        return None
    if cell != (row, col):
        print("[Zone] 막힌 자리 -> 가까운 빈 자리로 옮겼다")
    zone = store.place(*grid.to_world(*cell))
    if zone is None:
        print(
            f"[Zone] 라벨을 다 썼다 ({', '.join(store.allowed_labels)}) — config.yaml 의 zones.ids 를 늘린다"
        )
        return None
    print(f"[Zone] {zone.label} = ({zone.x:.2f}, {zone.y:.2f}) m — 바라볼 방향을 클릭한다")
    return zone


def aim(store: ZoneStore, label: str, x: float, y: float) -> Zone:
    """구역 자리에서 클릭 지점을 바라보게 한다."""
    zone = store.get(label)
    zone = store.aim(label, math.atan2(y - zone.y, x - zone.x))
    print(f"[Zone] {zone.label} 방향 = {math.degrees(zone.yaw or 0.0):+.0f}°")
    return zone


def run(args: argparse.Namespace, config: dict) -> int:
    maps = Path(args.maps) if args.maps else settings.maps_dir(config)
    labels = tuple(str(label) for label in config["zones"]["ids"])
    store = ZoneStore.load(maps, labels)

    if args.list:
        if not len(store):
            print(f"[Zone] 저장된 구역이 없다: {maps / 'zones.json'}")
            return 0
        for zone in store.as_tuple():
            facing = "방향 없음" if zone.yaw is None else f"방향 {math.degrees(zone.yaw):+.0f}°"
            print(f"  {zone.label} = ({zone.x:.3f}, {zone.y:.3f}) m · {facing}")
        print(f"[Zone] {len(store)} / {len(labels)} 개 지정됨")
        return 0

    grid = OccupancyGrid.load(maps)
    free_thresh = float(config["lidar"]["free_logodds"])

    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(6, 6))
    #: 자리는 찍고 방향을 아직 안 찍은 구역
    aiming: list[str] = []

    def redraw() -> None:
        axes.clear()
        axes.imshow(
            to_image(grid),
            cmap="gray",
            vmin=0,
            vmax=1,
            origin="lower",
            extent=grid.extent,
        )
        for zone in store.as_tuple():
            axes.plot(zone.x, zone.y, "ro", markersize=8)
            if zone.yaw is not None:
                axes.arrow(
                    zone.x,
                    zone.y,
                    0.3 * math.cos(zone.yaw),
                    0.3 * math.sin(zone.yaw),
                    color="red",
                    head_width=0.08,
                )
            axes.annotate(
                zone.label,
                (zone.x, zone.y),
                color="red",
                fontsize=12,
                fontweight="bold",
                xytext=(4, 4),
                textcoords="offset points",
            )
        if aiming:
            axes.set_title(f"구역 {aiming[0]} — 바라볼 방향을 클릭 / u·s")
        else:
            remaining = store.next_label or "없음"
            axes.set_title(f"구역 지정 — 다음 라벨: {remaining} / 클릭·u·s")
        figure.canvas.draw_idle()

    def on_click(event) -> None:
        if event.button != 1 or event.inaxes is not axes:
            return
        x, y = float(event.xdata), float(event.ydata)
        if aiming:
            aim(store, aiming.pop(), x, y)
        else:
            zone = place(store, grid, x, y, free_thresh)
            if zone is not None:
                aiming.append(zone.label)
        redraw()

    def on_key(event) -> None:
        if event.key == "u":
            removed = store.undo()
            aiming.clear()
            if removed is not None:
                print(f"[Zone] {removed.label} 취소")
            redraw()
        elif event.key == "s":
            store.save(maps)
            print(f"[Zone] 저장 ({len(store)}개)")

    figure.canvas.mpl_connect("button_press_event", on_click)
    figure.canvas.mpl_connect("key_press_event", on_key)
    # 창을 닫아도 저장한다 — 클릭을 다 하고 저장을 잊는 것이 가장 흔한 실수다.
    figure.canvas.mpl_connect("close_event", lambda _event: store.save(maps))

    redraw()
    print(f"[Zone] 라벨: {', '.join(labels)} — 왼쪽 클릭 두 번(자리 → 방향)으로 순서대로 붙인다")
    plt.show()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="순찰 구역 좌표화 — zones.json 을 만든다")
    parser.add_argument("--maps", default=None, help="지도·구역 경로. 기본은 maps/")
    parser.add_argument("--list", action="store_true", help="저장된 구역만 출력한다")
    return parser


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **인자 처리보다 앞이다** — cp949 콘솔에서 `--help` 조차 죽었다
    # (CONTRIBUTING 8절). 도움말은 `argparse` 가 stdout 에 쓴다.
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    try:
        config = settings.load(None)
        setup_logging(config, device_id="zone-select", console=False)
        return run(args, config)
    except ConfigError as exc:
        print(f"[Zone] 설정 오류: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[Zone] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
