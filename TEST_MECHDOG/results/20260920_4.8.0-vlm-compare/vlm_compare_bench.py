"""Qwen2-VL-2B 두 장 비교 실측 (WBS 4.8.0 · ADR-35 재검토).

물음은 넷이고 **정답이 자명한 것부터** 묻는다 — 라벨링에 사람 주관이 들어가면
숫자를 믿을 수 없다.

    ① 두 장을 구분하는가      같은 장 두 개 vs 다른 장 두 개. 정답 자명
    ② 변화 없음에 조용한가    같은 세션 연속 프레임. 기대 «no»
    ③ 변화 있음을 말하는가    다른 세션 프레임. 기대 «yes» + 서술
    ④ 단일 프레임 기준선      기존 3질문 (비교 없이)

속도와 VRAM 을 항목마다 잰다. 이미지는 결과에 복사하지 않는다 (얼굴 포함 가능).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

MODEL_ID = "Qwen/Qwen2-VL-2B-Instruct"
REPO = Path(r"C:\Users\pjw\Desktop\mechdog_physical_ai")


def vram_gb() -> float:
    if not torch.cuda.is_available():
        return 0.0
    return round(torch.cuda.memory_allocated() / 1024**3, 2)


def peak_gb() -> float:
    if not torch.cuda.is_available():
        return 0.0
    return round(torch.cuda.max_memory_allocated() / 1024**3, 2)


def load():
    started = time.monotonic()
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        MODEL_ID, dtype=torch.bfloat16, device_map="cuda"
    )
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    return model, processor, time.monotonic() - started


def ask(model, processor, images: list[Path], prompt: str) -> tuple[str, float]:
    content = [{"type": "image"} for _ in images]
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    pil = [Image.open(p).convert("RGB") for p in images]
    inputs = processor(text=[text], images=pil, return_tensors="pt").to("cuda")
    started = time.monotonic()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=96, do_sample=False)
    elapsed = time.monotonic() - started
    trimmed = out[0][inputs["input_ids"].shape[1] :]
    answer = processor.decode(trimmed, skip_special_tokens=True).strip()
    return answer, elapsed


def sessions() -> list[list[Path]]:
    files = sorted(
        (REPO / "blackbox").glob("*_person_found/snapshot.jpg"),
        key=lambda f: int(f.parent.name.split("_")[0]),
    )
    groups: list[list[Path]] = []
    prev: int | None = None
    for f in files:
        stamp = int(f.parent.name.split("_")[0])
        if prev is None or stamp - prev > 600_000:
            groups.append([])
        groups[-1].append(f)
        prev = stamp
    return groups


def main() -> int:
    groups = sessions()
    groups.sort(key=len, reverse=True)
    long_a, long_b = groups[0], groups[1]

    cases: list[dict] = [
        # ① 두 장을 구분하는가 — 정답 자명
        {
            "id": "1a_identical",
            "kind": "two",
            "images": [long_a[0], long_a[0]],
            "prompt": "Are these two images identical? Answer with yes or no only.",
            "expected": "yes",
        },
        {
            "id": "1b_different",
            "kind": "two",
            "images": [long_a[0], long_b[0]],
            "prompt": "Are these two images identical? Answer with yes or no only.",
            "expected": "no",
        },
        # ② 변화 없음에 조용한가 — 같은 세션 연속 프레임
        {
            "id": "2a_consecutive",
            "kind": "two",
            "images": [long_a[0], long_a[1]],
            "prompt": (
                "The first image is the reference and the second is the current view of the "
                "same place. Did any object appear, disappear, or fall over? "
                "Answer with yes or no only."
            ),
            "expected": "no",
        },
        {
            "id": "2b_consecutive_other",
            "kind": "two",
            "images": [long_b[0], long_b[1]],
            "prompt": (
                "The first image is the reference and the second is the current view of the "
                "same place. Did any object appear, disappear, or fall over? "
                "Answer with yes or no only."
            ),
            "expected": "no",
        },
        # ③ 변화 있음 — 다른 세션 (서술까지 본다)
        {
            "id": "3a_cross_session_yesno",
            "kind": "two",
            "images": [long_a[0], long_a[-1]],
            "prompt": (
                "The first image is the reference and the second is the current view of the "
                "same place. Did any object appear, disappear, or fall over? "
                "Answer with yes or no only."
            ),
            "expected": "?",
        },
        {
            "id": "3b_cross_session_describe",
            "kind": "two",
            "images": [long_a[0], long_a[-1]],
            "prompt": (
                "The first image is the reference and the second is the current view of the "
                "same place. Describe in one sentence what changed."
            ),
            "expected": "?",
        },
        # ④ 단일 프레임 기준선 — 지금 vlm_reader.py 가 쓰는 질문 그대로
        {
            "id": "4a_person_down",
            "kind": "one",
            "images": [long_a[0]],
            "prompt": "Is there a person lying on the floor? Answer with yes or no only.",
            "expected": "no",
        },
        {
            "id": "4b_fallen_object",
            "kind": "one",
            "images": [long_a[0]],
            "prompt": (
                "Is there an object that has fallen over or collapsed? Answer with yes or no only."
            ),
            "expected": "?",
        },
        {
            "id": "4c_blocked_path",
            "kind": "one",
            "images": [long_a[0]],
            "prompt": "Is the walkway blocked by an obstacle? Answer with yes or no only.",
            "expected": "?",
        },
    ]

    print(f"적재 중… ({MODEL_ID}, bf16, cuda)", flush=True)
    model, processor, load_s = load()
    print(f"적재 {load_s:.1f}초 · VRAM {vram_gb()} GB", flush=True)

    results = []
    for case in cases:
        answer, elapsed = ask(model, processor, case["images"], case["prompt"])
        row = {
            "id": case["id"],
            "kind": case["kind"],
            "images": [str(p.relative_to(REPO)) for p in case["images"]],
            "prompt": case["prompt"],
            "expected": case["expected"],
            "answer": answer,
            "seconds": round(elapsed, 2),
            "vram_peak_gb": peak_gb(),
        }
        results.append(row)
        print(
            f"  [{row['id']:26}] {elapsed:5.2f}s  기대={row['expected']:3}  답={answer[:90]}",
            flush=True,
        )

    summary = {
        "model": MODEL_ID,
        "dtype": "bfloat16",
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
        "load_seconds": round(load_s, 2),
        "vram_after_load_gb": vram_gb(),
        "vram_peak_gb": peak_gb(),
        "cases": results,
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("vlm_compare_results.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n원자료 → {out}")
    print(f"적재 {load_s:.1f}s · VRAM 적재후 {vram_gb()}GB · 최대 {peak_gb()}GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
