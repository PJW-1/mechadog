"""LoRA 어댑터를 bf16 기본 모델에 병합해 폴더 하나로 저장한다 (ADR-35 · 무양자화).

    ~/.venv-mechdog-vlm/Scripts/python.exe tools/vlm_lora/merge.py <어댑터폴더> --out <병합폴더>

병합 폴더에는 가중치(safetensors)·프로세서(학습 때의 픽셀 상한 포함)·`lora_merge.json`
(기본 모델·어댑터 경로·학습 설정·목록 해시)이 들어간다. 운용은 `vision.vlm.model_id` 에 이
폴더의 **절대 경로**를 주면 읽는다 — `from_pretrained` 가 모델 ID 와 로컬 폴더를 둘 다 받는다.

⚠️ 병합 폴더는 약 4.5GB 다. 저장소 밖에 둔다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.console import survive_encoding_errors  # noqa: E402
from tools.vlm_lora.train import CONFIG_NAME  # noqa: E402

RECORD_NAME = "lora_merge.json"


def merge_record(adapter_dir: Path) -> dict[str, Any]:
    """병합 기록. 어댑터의 `train_config.json` 이 없으면 `FileNotFoundError`."""
    config = json.loads((adapter_dir / CONFIG_NAME).read_text(encoding="utf-8"))
    return {
        "base_model": config["base_model"],
        "adapter": str(adapter_dir.resolve()),
        "dataset_sha256": config["dataset_sha256"],
        "dtype": "bfloat16",
        "merged_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "train_config": config,
    }


def merge(base: str, adapter_dir: Path, out: Path) -> None:  # pragma: no cover - GPU 병합
    import torch
    from peft import PeftModel
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    model = Qwen2VLForConditionalGeneration.from_pretrained(
        base, dtype=torch.bfloat16, device_map="cpu"
    )
    model = PeftModel.from_pretrained(model, str(adapter_dir)).merge_and_unload()
    model.save_pretrained(out, safe_serialization=True)
    # 어댑터 폴더의 프로세서 — 학습 때의 픽셀 상한을 운용으로 그대로 잇는다.
    AutoProcessor.from_pretrained(str(adapter_dir)).save_pretrained(out)


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("adapter", type=Path, help="train.py 의 출력 폴더")
    parser.add_argument("--out", type=Path, required=True, help="병합 폴더 (없어야 한다)")
    args = parser.parse_args(argv)
    if args.out.exists():
        print(f"{args.out}: 이미 있다 — 병합 모델을 덮지 않는다", file=sys.stderr)
        return 2
    try:
        record = merge_record(args.adapter)
    except FileNotFoundError:
        print(f"{args.adapter}: {CONFIG_NAME} 가 없다 — train.py 의 출력이 아니다", file=sys.stderr)
        return 2
    merge(record["base_model"], args.adapter, args.out)
    (args.out / RECORD_NAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"병합 → {args.out} · vision.vlm.model_id: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
