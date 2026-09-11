"""사원증 ArUco 마커를 인쇄용으로 만든다 (WBS 3.8.1 · FR-10.1).

    python tools/make_badges.py                 # datasets/badges/ 에 A4 300dpi
    python tools/make_badges.py --side-cm 20    # 더 멀리서 읽어야 할 때

⚠️ **이미지를 저장소에 넣지 않는다.** `config.yaml` 의 `badge_marker_map` 이
정본이고 여기서 그때그때 만든다 — 가중치를 `fetch_models.py` 로 받는 것과 같은
이유다. 발급 대장을 고치고 이미지를 다시 만들지 않으면 **인쇄물과 설정이 어긋난
채로 아무도 모른다.**

⚠️ **100% 로 인쇄해야 한다.** *"용지에 맞춤"* 을 켜면 크기가 달라지고, 그러면
아래 거리 표가 무의미해진다. 파일에 300dpi 를 박아 두었으므로 대부분의 인쇄
대화상자에서 실제 크기가 잡힌다.

⚠️ **자발광 화면(폰·모니터)은 사원증 대역으로 쓸 수 없다.** 2026-09-11 실기에서
확인했다 — 밝기를 낮추면 검은 칸이 회색으로 떠 대비가 2:1 미만이 되고, 올리면
화면이 포화돼 무늬가 사라진다. 유리 반사가 칸 일부를 덮고, 렌즈 최소 초점거리
안쪽에서는 번진다. 사각형 후보는 찾지만 **비트가 해독되지 않는다.** 종이는
확산 반사라 방과 같은 노출로 잡히고 잉크가 실제로 어둡다 (PRD `A-16`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_base_config  # noqa: E402

DPI = 300
#: A4 세로. 인치 → 픽셀.
A4_PX = (int(8.27 * DPI), int(11.69 * DPI))
#: 마커 변 길이 대비 흰 여백. **없으면 검출되지 않는다** — ArUco 는 테두리 바깥의
#: 밝은 영역으로 마커를 찾는다.
QUIET_RATIO = 0.18
#: 거절 경로(FR-10.3)를 실기로 시험할 미등록 ID. 대장에 없어야 의미가 있다.
UNREGISTERED_ID = 7


def sheet(dictionary: object, marker_id: int, label: str, side_cm: float) -> np.ndarray:
    """마커 한 장을 A4 에 올린다."""
    side = int(side_cm / 2.54 * DPI)
    quiet = int(side * QUIET_RATIO)
    block = side + quiet * 2
    tile = np.full((block, block), 255, np.uint8)
    tile[quiet : quiet + side, quiet : quiet + side] = cv2.aruco.generateImageMarker(
        dictionary, marker_id, side
    )
    page = np.full((A4_PX[1], A4_PX[0]), 255, np.uint8)
    x, y = (A4_PX[0] - block) // 2, int(2.0 / 2.54 * DPI)
    page[y : y + block, x : x + block] = tile
    cv2.putText(page, label, (x, y + block + 120), cv2.FONT_HERSHEY_SIMPLEX, 1.8, 0, 4)
    cv2.putText(
        page,
        "print at 100% (do not scale to fit)",
        (x, y + block + 240),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.3,
        0,
        3,
    )
    return page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_badges", description="사원증 마커 생성")
    parser.add_argument("--out", type=Path, default=Path("datasets/badges"))
    parser.add_argument("--side-cm", type=float, default=14.0, help="마커 변 길이 (기본 14cm)")
    args = parser.parse_args(argv)

    auth = load_base_config()["auth"]
    name = str(auth["badge_dictionary"])
    code = getattr(cv2.aruco, name, None)
    if code is None:
        print(f"auth.badge_dictionary 를 cv2.aruco 에서 찾을 수 없다: {name}", file=sys.stderr)
        return 2
    dictionary = cv2.aruco.getPredefinedDictionary(code)

    args.out.mkdir(parents=True, exist_ok=True)
    pages = [(int(k), str(v)) for k, v in (auth["badge_marker_map"] or {}).items()]
    pages.append((UNREGISTERED_ID, "UNREGISTERED (rejection test)"))
    for marker_id, holder in pages:
        label = f"{holder}   /   {name}   ID {marker_id}   /   {args.side_cm:.0f}cm"
        path = args.out / f"badge_{marker_id}.png"
        Image.fromarray(sheet(dictionary, marker_id, label, args.side_cm)).save(
            path, dpi=(DPI, DPI)
        )
        print(f"{path}  (ID {marker_id} · {holder})")

    # 거리별로 몇 픽셀이 되는지 함께 알려준다 — 실측 하한은 12px 다.
    fov_deg = 65.0  # OV3660 수평 화각 (대략)
    half = np.tan(np.radians(fov_deg) / 2)
    print(f"\n마커 {args.side_cm:.0f}cm 가 VGA(640px) 에서 잡히는 크기")
    for dist_m in (1.0, 1.5, 2.0, 2.5, 3.0):
        width_cm = 2 * dist_m * half * 100
        print(f"  {dist_m:.1f}m  {args.side_cm / width_cm * 640:5.0f}px")
    print("검출 하한은 12px 로 실측했다 (JPEG 압축 후에도 동일).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
