"""ppe_seed_labels 의 판정 로직 시험 — 합성 프레임으로.

검증하는 것은 모델이 아니라 **규칙**이다 — 색 비율→클래스 매핑,
확신 없음 대역, 머리 잘림 보류가 규약대로 동작하는지.
"""

from __future__ import annotations

import numpy as np

from host.vision.detector import Detection
from tools.ppe_seed_labels import Band, seed_labels

W, H = 640, 480
PERSON = Detection(label="person", score=0.9, box=(200.0, 60.0, 440.0, 460.0))
# 머리가 프레임 상단에 잘린 사람 — y1 이 margin 안이다.
CLIPPED = Detection(label="person", score=0.9, box=(200.0, 0.0, 440.0, 460.0))

ORANGE_BGR = (30, 100, 255)  # BGR — 주황 안전모색
LIME_BGR = (60, 220, 180)  # BGR — 연두 형광 조끼색


def _blank() -> np.ndarray:
    return np.zeros((H, W, 3), dtype=np.uint8)


def _paint_person(img: np.ndarray, box: Detection, head=None, torso=None) -> np.ndarray:
    x1, y1, x2, y2 = (int(v) for v in box.box)
    h = y2 - y1
    if head is not None:
        img[y1 : int(y1 + h * 0.25), x1:x2] = head
    if torso is not None:
        img[int(y1 + h * 0.25) : int(y1 + h * 0.8), x1:x2] = torso
    return img


def _classes(lines: list[str]) -> set[int]:
    return {int(line.split()[0]) for line in lines}


def test_helmet_and_vest_detected_by_color() -> None:
    img = _paint_person(_blank(), PERSON, head=ORANGE_BGR, torso=LIME_BGR)
    lines, evidence = seed_labels(img, [PERSON])
    assert _classes(lines) == {0, 2}  # helmet + vest
    verdicts = {ev["class"] for ev in evidence}
    assert verdicts == {"helmet", "vest"}


def test_no_ppe_means_no_classes() -> None:
    lines, evidence = seed_labels(_blank(), [PERSON])
    assert _classes(lines) == {1, 3}  # no_helmet + no_vest
    assert {ev["class"] for ev in evidence} == {"no_helmet", "no_vest"}


def test_uncertain_band_writes_no_label() -> None:
    """대역 사이에 들어오면 라벨을 쓰지 않고 사람에게 넘긴다."""
    # 영역 절반만 칠해 비율이 off~on 사이에 떨어지게 한다.
    img = _blank()
    x1, y1, x2, y2 = (int(v) for v in PERSON.box)
    h = y2 - y1
    mid_x = (x1 + x2) // 2
    img[y1 : int(y1 + h * 0.25), x1:mid_x] = ORANGE_BGR  # 머리대의 절반만
    img[int(y1 + h * 0.25) : int(y1 + h * 0.8), x1:mid_x] = LIME_BGR
    lines, evidence = seed_labels(
        img, [PERSON], bands={"helmet": Band(0.99, 0.02), "vest": Band(0.99, 0.02)}
    )
    assert lines == []
    assert {ev["class"] for ev in evidence} == {"uncertain"}


def test_clipped_head_defers_helmet_but_judges_vest() -> None:
    """머리가 잘리면 헬멧 판정은 보류, 몸통은 정상 판정한다."""
    img = _paint_person(_blank(), CLIPPED, torso=LIME_BGR)
    lines, evidence = seed_labels(img, [CLIPPED])
    assert _classes(lines) == {2}  # vest 만
    verdicts = [ev["class"] for ev in evidence]
    assert "deferred" in verdicts and "vest" in verdicts


def test_person_crop_zones_stay_inside_person_box() -> None:
    """라벨 박스가 person 박스를 벗어나지 않는다 — 수평 마진 확인."""
    img = _paint_person(_blank(), PERSON, head=ORANGE_BGR, torso=LIME_BGR)
    lines, _ = seed_labels(img, [PERSON])
    pw, ph = (PERSON.box[2] - PERSON.box[0]) / W, (PERSON.box[3] - PERSON.box[1]) / H
    for line in lines:
        _, cx, cy, w, h = (float(v) for v in line.split())
        assert w < pw and h < ph  # 영역 라벨은 person 박스보다 작아야 한다
