"""사원증 마커 인쇄물 생성 (`tools/ops/make_badges.py`, WBS 3.8.1 · FR-10.1).

이미지는 저장소에 두지 않고 `config.yaml` 의 대장에서 그때그때 만든다 — 그래서 «만든 장이
실제로 해독되고 대장과 같은 ID 인가» 가 이 도구의 계약이다. 카메라·실기 없이 닫힌다.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from host.common.config import load_base_config
from tools.ops import make_badges


def _decode(png: Path, dictionary_name: str) -> list[int]:
    page = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
    assert page is not None
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary_name))
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    _corners, ids, _rejected = detector.detectMarkers(page)
    return [] if ids is None else [int(i) for i in ids.flatten()]


def test_sheet_is_a4_with_white_quiet_zone() -> None:
    """A4 300dpi 한 장에 마커를 가운데 올리고, 마커 둘레는 흰 여백이다 (없으면 검출 불가)."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    page = make_badges.sheet(dictionary, 3, "label", 4.0)
    assert page.shape == (make_badges.A4_PX[1], make_badges.A4_PX[0])
    side = int(4.0 / 2.54 * make_badges.DPI)
    quiet = int(side * make_badges.QUIET_RATIO)
    x = (make_badges.A4_PX[0] - (side + quiet * 2)) // 2
    y = int(2.0 / 2.54 * make_badges.DPI)
    assert (page[y : y + quiet, x : x + side + quiet * 2] == 255).all()  # 위쪽 여백
    assert (page[y + quiet : y + quiet + side, x + quiet : x + quiet + side] < 255).any()  # 마커


def test_main_writes_registered_badges_and_one_unregistered_decodable(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """대장의 ID 마다 PNG 한 장 + 거절 시험용 미등록 ID 한 장을 만들고, 모두 같은 ID 로 해독된다."""
    auth = load_base_config()["auth"]
    registered = {int(k) for k in (auth["badge_marker_map"] or {})}
    assert make_badges.UNREGISTERED_ID not in registered  # 대장에 있으면 거절 시험이 무의미하다

    assert make_badges.main(["--out", str(tmp_path), "--side-cm", "8"]) == 0

    expected = registered | {make_badges.UNREGISTERED_ID}
    produced = {int(p.stem.split("_")[1]) for p in tmp_path.glob("badge_*.png")}
    assert produced == expected
    for marker_id in expected:
        assert _decode(tmp_path / f"badge_{marker_id}.png", str(auth["badge_dictionary"])) == [
            marker_id
        ]
    out = capsys.readouterr().out
    assert "검출 하한은 12px" in out
    assert "UNREGISTERED" in out


def test_main_distance_table_shrinks_with_distance(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """먼 거리일수록 VGA 에서 마커가 작게 잡힌다는 안내표가 단조 감소한다."""
    make_badges.main(["--out", str(tmp_path), "--side-cm", "14"])
    rows = [
        line.split() for line in capsys.readouterr().out.splitlines() if line.strip().endswith("px")
    ]
    sizes = [float(r[-1].removesuffix("px")) for r in rows if r[0].endswith("m")]
    assert len(sizes) == 5
    assert sizes == sorted(sizes, reverse=True)
    assert np.all(np.diff(sizes) < 0)


def test_main_rejects_unknown_dictionary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """설정의 사전 이름이 cv2.aruco 에 없으면 2 로 끝나고 아무것도 쓰지 않는다."""
    cfg = load_base_config()
    cfg["auth"] = dict(cfg["auth"], badge_dictionary="DICT_NOPE")
    monkeypatch.setattr(make_badges, "load_base_config", lambda: cfg)
    assert make_badges.main(["--out", str(tmp_path / "x")]) == 2
    assert "DICT_NOPE" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()
