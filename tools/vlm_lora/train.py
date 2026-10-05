"""Qwen2-VL LoRA 학습 — 비전 인코더는 얼리고 언어 모델 투영에만 LoRA (ADR-43 «재검토»).

    ~/.venv-mechdog-vlm/Scripts/python.exe tools/vlm_lora/train.py list.jsonl --out <어댑터폴더>

- 기본 모델은 운용 설정(`vision.vlm.model_id`)이고 bf16 으로 올린다 (ADR-35 · 무양자화).
- 프롬프트는 운용과 같은 문장(실행할 때 `vlm_reader.QUESTIONS` 에서 읽는다)과 같은 채팅
  형식(`vlm_session.chat_messages`)이다. 정답은 `Yes`/`No` 한 마디와 턴 끝 `<|im_end|>` 이고
  **손실은 그 토큰에만** 건다 — 프롬프트·이미지 토큰은 `-100` 이다(프롬프트만 토큰화한 길이로 자른다).
- 배치 1 + 누적(`--accum`), gradient checkpointing, 이미지 픽셀 상한(`--max-pixels`).
  상한은 프로세서 설정으로 어댑터 폴더에 함께 저장되고, 병합 폴더로 이어져 운용도 같은
  해상도로 읽는다.
- 학습 세트(`split: train`)만 쓴다. 보류·미상은 읽지 않는다.

출력 폴더: PEFT 어댑터 · 프로세서 · `train_config.json`(인자·목록 해시·질문별 예/아니오 수).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.config import load_base_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.vision.vlm_session import chat_messages, to_image  # noqa: E402
from tools.vlm_lora.dataset import (  # noqa: E402
    Entry,
    ListError,
    MissingQuestionError,
    file_sha256,
    question_for,
    read_jsonl,
)

#: 학습 정답 글. `parse_answer` 가 그대로 참/거짓으로 읽는다.
ANSWER_TEXT = {"yes": "Yes", "no": "No"}
IGNORE = -100
#: Qwen2-VL 채팅 템플릿이 assistant 턴을 닫는 토큰 — 이것까지 가르쳐야 답 뒤에서 멈춘다.
TURN_END = "<|im_end|>"
CONFIG_NAME = "train_config.json"
_ATTN = ("q_proj", "k_proj", "v_proj", "o_proj")
_MLP = ("gate_proj", "up_proj", "down_proj")


def target_modules(*, mlp: bool) -> str:
    """LoRA 를 붙일 모듈 이름 정규식(PEFT 는 `re.fullmatch`). `visual` 아래는 빼서 얼린다."""
    names = _ATTN + (_MLP if mlp else ())
    return rf"^(?!.*\bvisual\b).*\.({'|'.join(names)})$"


class ExampleError(ValueError):
    """학습 예제를 만들 수 없다 — 토크나이저·템플릿이 이 도구의 가정과 다르다."""


def answer_labels(
    input_ids: Sequence[int], prompt_ids: Sequence[int], turn_end_ids: Sequence[int]
) -> list[int]:
    """프롬프트 토큰을 가린 라벨. 프롬프트가 입력의 앞머리가 아니거나, 뒤가 비었거나,
    턴 끝 토큰으로 안 끝나면 `ExampleError`.

    정답 토큰을 끝에서 맞춰 찾지 않고 프롬프트만 토큰화한 결과로 자른다. 프롬프트 끝과 답 첫
    글자가 한 토큰으로 묶이면 길이로 자른 라벨은 그 토큰을 가려 답의 나머지만 가르친다 — 그래서
    앞머리가 그대로인지 비교해 멈춘다.
    """
    ids = list(input_ids)
    prompt = list(prompt_ids)
    if ids[: len(prompt)] != prompt:
        raise ExampleError(
            "프롬프트 끝과 정답 첫 글자가 한 토큰으로 묶였다 — 정답만 가르칠 수 없다"
        )
    if len(ids) <= len(prompt):
        raise ExampleError("프롬프트 뒤에 정답 토큰이 없다 — 토큰 경계가 어긋났다")
    n = len(turn_end_ids)
    if n == 0 or ids[-n:] != list(turn_end_ids):
        raise ExampleError(f"입력이 턴 끝 토큰 {TURN_END} 으로 끝나지 않는다")
    return [IGNORE] * len(prompt) + ids[len(prompt) :]


def build_example(processor: Any, entry: Entry, *, image: Any) -> tuple[Any, list[int]]:
    """운용 프롬프트 + 정답 한 마디를 토큰으로. (프로세서 출력, 라벨 목록)을 돌려준다."""
    if entry.answer not in ANSWER_TEXT:
        raise ExampleError(f"정답이 yes·no 가 아니다: {entry.answer!r} ({entry.image})")
    prompt = processor.apply_chat_template(
        chat_messages(question_for(entry.key).prompt),
        tokenize=False,
        add_generation_prompt=True,
    )
    answer = ANSWER_TEXT[entry.answer] + TURN_END
    # 같은 이미지로 프롬프트만 토큰화한다 — 이미지 패드 토큰 수가 같아야 앞머리가 맞는다.
    prompt_out = processor(text=[prompt], images=[image], return_tensors="pt")
    prompt_ids = [int(token) for token in prompt_out["input_ids"][0]]
    inputs = processor(text=[prompt + answer], images=[image], return_tensors="pt")
    ids = [int(token) for token in inputs["input_ids"][0]]
    turn_end_ids = processor.tokenizer(TURN_END, add_special_tokens=False)["input_ids"]
    return inputs, answer_labels(ids, prompt_ids, turn_end_ids)


def training_entries(entries: Sequence[Entry], keys: Sequence[str] | None) -> list[Entry]:
    return [
        entry
        for entry in entries
        if entry.split == "train"
        and entry.answer in ANSWER_TEXT
        and (keys is None or entry.key in keys)
    ]


def run_config(args: argparse.Namespace, entries: Sequence[Entry]) -> dict[str, Any]:
    """`train_config.json` 의 내용. 병합 기록이 이것을 그대로 옮긴다."""
    examples: dict[str, dict[str, int]] = {}
    for (key, answer), count in sorted(Counter((e.key, e.answer) for e in entries).items()):
        examples.setdefault(key, {"yes": 0, "no": 0})[str(answer)] = count
    return {
        "base_model": args.base,
        "dataset": str(args.dataset.resolve()),
        "dataset_sha256": file_sha256(args.dataset),
        "keys": args.keys,
        "examples": examples,
        "epochs": args.epochs,
        "lr": args.lr,
        "accum": args.accum,
        "seed": args.seed,
        "max_pixels": args.max_pixels,
        "dtype": "bfloat16",
        "lora": {
            "r": args.rank,
            "alpha": args.alpha,
            "dropout": args.dropout,
            "target_modules": target_modules(mlp=args.mlp),
        },
    }


def train(
    args: argparse.Namespace, entries: Sequence[Entry], config: dict[str, Any]
) -> None:  # pragma: no cover - GPU 학습
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    torch.manual_seed(args.seed)
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        args.base, dtype=torch.bfloat16, device_map="cuda"
    )
    processor = AutoProcessor.from_pretrained(args.base, max_pixels=args.max_pixels)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    lora = config["lora"]
    model = get_peft_model(
        model,
        LoraConfig(
            r=lora["r"],
            lora_alpha=lora["alpha"],
            lora_dropout=lora["dropout"],
            target_modules=lora["target_modules"],
            task_type="CAUSAL_LM",
        ),
    )
    trainable = [name for name, p in model.named_parameters() if p.requires_grad]
    if any("visual" in name for name in trainable):
        raise RuntimeError("비전 인코더가 학습 대상에 들어갔다")
    model.print_trainable_parameters()
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr)
    order = list(entries)
    rng = random.Random(args.seed)
    model.train()
    step = 0
    for epoch in range(args.epochs):
        rng.shuffle(order)
        total = 0.0
        for index, entry in enumerate(order, start=1):
            inputs, labels = build_example(
                processor, entry, image=to_image(Path(entry.image).read_bytes())
            )
            inputs = inputs.to("cuda")
            label_tensor = torch.tensor([labels], device="cuda")
            loss = model(**inputs, labels=label_tensor).loss / args.accum
            loss.backward()
            total += float(loss) * args.accum
            if index % args.accum == 0 or index == len(order):
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                step += 1
        peak = torch.cuda.max_memory_allocated() / 2**30
        print(
            f"epoch {epoch + 1}/{args.epochs} · 평균 손실 {total / len(order):.4f} "
            f"· 갱신 {step} · 최대 VRAM {peak:.2f} GiB",
            flush=True,
        )
    config["peak_vram_gib"] = round(torch.cuda.max_memory_allocated() / 2**30, 2)
    model.save_pretrained(args.out)
    processor.save_pretrained(args.out)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("dataset", type=Path, help="dataset.py 가 만든 목록 JSONL")
    parser.add_argument("--out", type=Path, required=True, help="어댑터 폴더 (비어 있어야 한다)")
    parser.add_argument("--base", help="기본 모델 (기본: 운용 설정 vision.vlm.model_id)")
    parser.add_argument("--keys", nargs="+", help="학습할 질문키 (기본: 목록의 전부)")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--accum", type=int, default=8, help="배치 1 의 기울기 누적 수")
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--alpha", type=int, default=32)
    parser.add_argument("--dropout", type=float, default=0.05)
    parser.add_argument("--mlp", action="store_true", help="MLP 투영에도 LoRA 를 붙인다")
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=640 * 480,
        help="이미지 픽셀 상한 (기본 VGA = 운용 원본 그대로 · VRAM 이 모자라면 낮춘다)",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.base is None:
        args.base = str(load_base_config()["vision"]["vlm"]["model_id"])
    return args


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    args = parse_args(argv)
    if args.out.exists() and any(args.out.iterdir()):
        print(f"{args.out}: 비어 있지 않다 — 어댑터를 덮지 않는다", file=sys.stderr)
        return 2
    try:
        entries = training_entries(read_jsonl(args.dataset), args.keys)
    except ListError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:  # GPU 를 만지기 전에 질문 문장부터 본다
        for key in dict.fromkeys([*(args.keys or ()), *(entry.key for entry in entries)]):
            question_for(key)
    except MissingQuestionError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not entries:
        print("학습할 사진이 없다 — split=train 이고 정답이 있는 줄이 없다", file=sys.stderr)
        return 2
    config = run_config(args, entries)
    print(json.dumps(config["examples"], ensure_ascii=False), flush=True)
    try:
        train(args, entries, config)
    except ExampleError as exc:  # 어느 예제에서 나든 어댑터는 루프가 끝난 뒤에야 쓴다
        print(exc, file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / CONFIG_NAME).write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"어댑터 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
