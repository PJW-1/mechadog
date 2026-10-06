"""기존 hazard-v1 과 새 모델을 로봇 실물 사진에서 비교한다 (런타임 Detector 그대로).

python -X utf8 compare.py [새 onnx]
"""

import copy
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from host.vision.detector import Detector  # noqa: E402
from host.vision.hazard_detector import HAZARD_CLASSES  # noqa: E402

REC = Path(__import__("os").environ.get("HZ_REC_DIR", "field_records/2026-10-06"))
# (폴더, 파일, 기대) — 기대: 실물 위험물이 보이는지 / 오검출 함정(의자·PC·쓰레기통)
CASES = [
    ("리허설3_1805", "1791278379698_2440.jpg", "보조배터리(눕힘)+쓰레기통 치우는 중"),
    ("리허설3_1805", "1791278381752_2492.jpg", "보조배터리 눕힘(학습에 크롭 사용)"),
    ("리허설3_1805", "1791278475551_4602.jpg", "보조배터리 세움(학습에 크롭 사용)"),
    ("리허설3_1805", "1791278561838_6590.jpg", "라이터 눕힘(멀고 작음)"),
    ("리허설3_1805", "1791278632467_8191.jpg", "라이터 세움(학습에 크롭 사용)"),
    ("리허설3_1805", "1791278274054_95.jpg", "흰 쓰레기통 + 보조배터리 잘림"),
    ("촬영2_1842", "1791279890348_3106.jpg", "보조배터리+라이터 바닥, 의자 함정"),
    ("리허설4_1825", "1791279015003_5617.jpg", "PC 본체 함정 + 라이터·보조배터리"),
    ("촬영1_1835", "1791279615067_4174.jpg", "의자 함정 + 라이터"),
]
cfg = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "config" / "config.yaml").read_text(encoding="utf-8")
)


def det(model_path):
    c = copy.deepcopy(cfg)
    c["vision"]["hazard"]["conf_threshold"] = 0.05
    c["vision"]["hazard"]["model_path"] = model_path
    return Detector(c, section="hazard", labels=HAZARD_CLASSES)


old = det(str(Path(__file__).resolve().parents[3] / "models" / "hazard.onnx"))
new = det(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "out" / "hazard.onnx"))
rows = []
for d, f, note in CASES:
    p = REC / d / "frames" / f
    img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print("없음", p)
        continue
    pair = []
    for name, m in (("v1", old), ("new", new)):
        r = [x for x in m.detect(img) if x.score >= 0.2]
        print(
            f"{name:3s} {f} {note}: "
            + ", ".join(f"{x.label} {x.score:.2f} {[int(v) for v in x.box]}" for x in r)
        )
        im = img.copy()
        for x in r:
            b = [int(v) for v in x.box]
            col = (0, 0, 255) if x.score >= 0.5 else (0, 200, 255)
            cv2.rectangle(im, (b[0], b[1]), (b[2], b[3]), col, 2)
            cv2.putText(
                im, f"{x.label[:3]} {x.score:.2f}", (b[0], max(14, b[1] - 3)), 0, 0.55, col, 2
            )
        cv2.putText(im, name, (8, 30), 0, 1, (255, 255, 255), 3)
        pair.append(cv2.resize(im, (480, 360)))
    rows.append(np.hstack(pair))
cv2.imencode(".jpg", np.vstack(rows))[1].tofile(str(Path(__file__).parent / "compare.jpg"))
