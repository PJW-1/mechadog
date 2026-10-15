"""학습된 YOLOX PPE 체크포인트를 ONNX 로 내보내고 **메타데이터 JSON** 을 옆에 쓴다.

두 단계이고, 돌리는 python 이 다르다.

    # ① 내보내기 + 원시 출력 parity — 학습 환경 python (torch·yolox·onnxruntime)
    C:\\dev\\ppe-train\\.venv\\Scripts\\python tools/ppe/export_ppe.py export `
        --exp tools/ppe/yolox_exp_ppe_s.py `
        --ckpt C:\\dev\\ppe-train\\YOLOX\\YOLOX_outputs\\yolox_exp_ppe_s\\best_ckpt.pth `
        --data-dir datasets/ppe/build/ppe4_cs_v1 `
        --out models/ppe_v4.onnx --version v4 --eval-result <eval 로그 경로>

    # ② 호스트 검출기로 확인 — 저장소 python (host.vision.detector 가 yaml 을 쓴다)
    python tools/ppe/export_ppe.py verify --device mechdog-01 `
        --onnx models/ppe_v4.onnx --images datasets/ppe/build/ppe4_cs_v1/val2017

메타데이터는 `models/ppe_v4.json` (onnx 와 같은 이름)에 쓰고 ②가 `host_check` 를 더한다.

⚠️ **출력은 디코드 전 원시 `[1, 8400, 9]` 다** (`decode_in_inference=False`). 호스트
어댑터가 격자 디코드를 한다 — 디코드된 모델을 넣으면 좌표가 두 번 변환된다
(config.yaml `vision.ppe` 주석). 9 = 오프셋 2 + 크기 2 + objectness 1 + 클래스 4.

⚠️ **opset 11 은 YOLOX `tools/export_onnx.py` 기본값을 따른다.** onnxsim 은 학습 환경에
없어 쓰지 않는다 — 단순화 없이도 onnxruntime 은 같은 값을 낸다(parity 로 확인).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import inspect
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

CLASSES = ("helmet", "no_helmet", "vest", "no_vest")
INPUT_SIZE = 640
STRIDES = (8, 16, 32)
OPSET = 11
INPUT_NAME, OUTPUT_NAME = "images", "output"
#: 원시 출력 최대 절대 오차 허용치. fp32 내보내기는 보통 1e-5 언저리다.
PARITY_TOL = 1e-3
PARITY_SEED = 20260928
PREPROCESS = (
    "BGR 순서 · 0~255 float32 그대로(0~1 정규화·평균 보정 없음) · letterbox 좌상단 정렬 · "
    "여백 114 · 리사이즈 크기는 int(버림) · INTER_LINEAR — host/vision/detector.YoloxAdapter"
)


def expected_output_shape(num_classes: int, input_size: int = INPUT_SIZE) -> list[int]:
    """YOLOX 원시 출력 모양 `[1, 후보 수, 5 + 클래스]`. 640 이면 후보 8400."""
    anchors = sum((input_size // s) ** 2 for s in STRIDES)
    return [1, anchors, 5 + num_classes]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_ref(path: Path | None) -> dict[str, Any] | None:
    """경로와 sha256. 파일이 없으면 경로만 남기고 표시한다."""
    if path is None:
        return None
    if not path.is_file():
        return {"path": path.as_posix(), "sha256": None, "missing": True}
    return {"path": path.as_posix(), "sha256": sha256_file(path)}


def iso_mtime(path: Path) -> str:
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return stamp.isoformat(timespec="seconds")


def metadata_path(onnx: Path) -> Path:
    return onnx.with_suffix(".json")


def export(args: argparse.Namespace) -> int:
    """① torch → onnx, 모양 검사, 원시 출력 parity, 메타데이터."""
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    from torch import nn
    from yolox.exp import get_exp
    from yolox.models.network_blocks import SiLU
    from yolox.utils import replace_module

    exp = get_exp(str(args.exp), None)
    if exp.num_classes != len(CLASSES):
        raise SystemExit(f"exp 클래스 수 {exp.num_classes} ≠ {len(CLASSES)}")
    model = exp.get_model()
    # ⚠️ 우리 체크포인트다. optimizer 상태까지 든 YOLOX 형식이라 weights_only 로는 못 읽는다.
    ckpt = torch.load(str(args.ckpt), map_location="cpu", weights_only=False)
    model.load_state_dict(ckpt.get("model", ckpt))
    model.eval()
    # YOLOX export_onnx.py 와 같다 — nn.SiLU 를 onnx 호환 구현으로 바꾼다.
    model = replace_module(model, nn.SiLU, SiLU)
    model.head.decode_in_inference = False

    args.out.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 3, INPUT_SIZE, INPUT_SIZE)
    extra: dict[str, Any] = {}
    if "dynamo" in inspect.signature(torch.onnx.export).parameters:
        extra["dynamo"] = False  # YOLOX 관례(TorchScript 경로)를 따른다
    torch.onnx.export(
        model,
        dummy,
        str(args.out),
        input_names=[INPUT_NAME],
        output_names=[OUTPUT_NAME],
        opset_version=OPSET,
        **extra,
    )
    onnx.checker.check_model(onnx.load(str(args.out)))

    want = expected_output_shape(len(CLASSES))
    session = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(PARITY_SEED)
    inputs = [
        rng.uniform(0, 255, (1, 3, INPUT_SIZE, INPUT_SIZE)).astype(np.float32),
        np.full((1, 3, INPUT_SIZE, INPUT_SIZE), 114, dtype=np.float32),  # letterbox 여백만
    ]
    worst = 0.0
    shape: list[int] = []
    for tensor in inputs:
        (onnx_out,) = session.run(None, {INPUT_NAME: tensor})
        with torch.no_grad():
            torch_out = model(torch.from_numpy(tensor)).numpy()
        shape = list(onnx_out.shape)
        worst = max(worst, float(np.abs(onnx_out - torch_out).max()))
    if shape != want:
        raise SystemExit(f"출력 모양 {shape} ≠ {want} — decode_in_inference·입력 크기 확인")

    data_dir = args.data_dir or (Path(exp.data_dir) if exp.data_dir else None)
    card = data_dir / "data_card.json" if data_dir else None
    card_ref = file_ref(card)
    if card_ref and card and card.is_file():
        card_ref["dataset_version"] = json.loads(card.read_text(encoding="utf-8")).get(
            "dataset_version"
        )
    parity_ok = worst <= PARITY_TOL
    meta = {
        "name": args.out.stem,
        "version": args.version,
        "sha256": sha256_file(args.out),
        "size_bytes": args.out.stat().st_size,
        "family": "yolox-s",
        "opset": OPSET,
        "input": {
            "name": INPUT_NAME,
            "shape": [1, 3, INPUT_SIZE, INPUT_SIZE],
            "dtype": "float32",
            "preprocess": PREPROCESS,
        },
        "output": {
            "name": OUTPUT_NAME,
            "shape": shape,
            "layout": "cx·cy 오프셋 2 · log w·h 2 · objectness(sigmoid) 1 · 클래스(sigmoid) 4 — 격자 디코드 전",
        },
        "classes": list(CLASSES),
        "trained_at": iso_mtime(args.ckpt),
        "checkpoint": {
            **(file_ref(args.ckpt) or {}),
            "epoch": ckpt.get("start_epoch"),
            "best_ap": ckpt.get("best_ap"),
        },
        "dataset": {
            "data_dir": data_dir.as_posix() if data_dir else None,
            "train_ann": exp.train_ann,
            "val_ann": exp.val_ann,
            "test_ann": exp.test_ann,
            "data_card": card_ref,
            "xiao_cards": [file_ref(p) for p in sorted(data_dir.glob("xiao_*_card.json"))]
            if data_dir
            else [],
        },
        "training": {
            "exp_file": file_ref(args.exp),
            "max_epoch": exp.max_epoch,
            "no_aug_epochs": exp.no_aug_epochs,
            "seed": exp.seed,
            "depth": exp.depth,
            "width": exp.width,
            "input_size": list(exp.input_size),
            "mosaic_prob": exp.mosaic_prob,
            "mosaic_scale": list(exp.mosaic_scale),
            "enable_mixup": exp.enable_mixup,
            "hsv_gains": list(getattr(inspect.getmodule(type(exp)), "HSV_GAINS", ())),
            "pretrained": file_ref(args.pretrained),
        },
        "parity": {
            "max_abs_diff": worst,
            "tol": PARITY_TOL,
            "ok": parity_ok,
            "inputs": ["uniform 0~255 (seed 20260928)", "상수 114"],
            "runtime": f"onnxruntime {ort.__version__} CPU · torch {torch.__version__}",
        },
        "evaluation": file_ref(args.eval_result),
        "host_check": None,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": {"script": "tools/ppe/export_ppe.py", "sha256": sha256_file(Path(__file__))},
    }
    path = metadata_path(args.out)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"ONNX {args.out} · {shape} · parity 최대 오차 {worst:.2e} ({'통과' if parity_ok else '초과'})"
    )
    print(f"메타데이터 {path}")
    if not parity_ok:
        return 1
    print("다음: 저장소 python 으로 `export_ppe.py verify` 를 돌려 호스트 검출기 확인을 더한다")
    return 0


def pick_evenly(items: Sequence[Path], count: int) -> list[Path]:
    """정렬된 목록에서 고르게 `count` 개. 앞쪽 몇 장만 보면 한 장면만 본다."""
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def verify(args: argparse.Namespace) -> int:
    """② `host.vision.detector` 로 검증 이미지 몇 장을 돌려 박스가 나오는지 본다."""
    import cv2

    sys.path.insert(0, str(ROOT))
    from host.common.config import load_config
    from host.vision.detector import Detector
    from tools.ppe.index_raw import imread_any

    meta_file = metadata_path(args.onnx)
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if sha256_file(args.onnx) != meta["sha256"]:
        raise SystemExit("onnx sha256 이 메타데이터와 다르다 — export 를 다시 돌린다")

    config = load_config(args.device)
    config["vision"]["ppe"]["model_path"] = str(args.onnx.resolve())
    detector = Detector(config, section="ppe", labels=meta["classes"])
    files = sorted(
        p for p in args.images.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")
    )
    results = []
    for path in pick_evenly(files, args.count):
        image = imread_any(path, cv2.IMREAD_COLOR)
        if image is None:
            continue
        found = detector.detect(image)
        results.append(
            {"file": path.name, "boxes": len(found), "labels": sorted({d.label for d in found})}
        )
        print(f"  {path.name}: {len(found)}개 {results[-1]['labels']}")
    with_boxes = sum(1 for r in results if r["boxes"])
    meta["host_check"] = {
        "images_dir": args.images.as_posix(),
        "conf_threshold": config["vision"]["ppe"]["conf_threshold"],
        "images": len(results),
        "with_boxes": with_boxes,
        "results": results,
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"박스가 나온 이미지 {with_boxes}/{len(results)} → {meta_file}")
    return 0 if with_boxes else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="torch → onnx + parity + 메타데이터 (학습 환경)")
    ex.add_argument("--exp", type=Path, required=True)
    ex.add_argument("--ckpt", type=Path, required=True)
    ex.add_argument("--out", type=Path, required=True, help="예: models/ppe_v4.onnx")
    ex.add_argument("--version", required=True, help="예: v4")
    ex.add_argument("--data-dir", type=Path, help="기본은 exp 의 PPE_DATA_DIR")
    ex.add_argument("--pretrained", type=Path, help="출발 가중치 (예: weights/yolox_s.pth)")
    ex.add_argument("--eval-result", type=Path, help="YOLOX eval 출력 로그")

    ve = sub.add_parser("verify", help="호스트 검출기로 확인 (저장소 python)")
    ve.add_argument("--device", required=True)
    ve.add_argument("--onnx", type=Path, required=True)
    ve.add_argument("--images", type=Path, required=True, help="예: .../val2017")
    ve.add_argument("--count", type=int, default=8)

    args = parser.parse_args(argv)
    return export(args) if args.command == "export" else verify(args)


if __name__ == "__main__":
    sys.exit(main())
