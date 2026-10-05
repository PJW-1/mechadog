"""보류 세트로 기본 모델과 병합 모델을 같은 잣대로 잰다.

    ~/.venv-mechdog-vlm/Scripts/python.exe tools/vlm_lora/evaluate.py list.jsonl \
        --merged <병합폴더> --out eval.json
    ... --base-only            # 병합 전, 기본 모델만

- 목록의 `split: holdout` 이고 정답이 있는 줄만 쓴다(다른 날·다른 배치 · `dataset.py`).
- 두 모델 모두 운용과 같은 길로 묻는다: 설정(`vision.vlm`)에서 `model_id` 만 바꿔
  `build_session_factory` 로 세션을 만들고, `vlm_bench.measure`(운용의 `VlmReader`·질문
  문장·`parse_answer`)로 사진마다 그 질문 하나를 묻는다. 병합 폴더가 이 길로 읽히면 운용
  런타임도 같은 설정으로 읽는다.
- 판독 불가는 «예 아님» 으로 채점하고 따로 센다(`vlm_bench` 와 같다). 모델은 한 번에 하나만
  올리고 다 쓰면 내린다.

⚠️ 가중치를 **내려받지 않는다** (`HF_HUB_OFFLINE=1`). 기본 모델이 캐시에 없으면 적재에서 멈춘다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.config import load_base_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.vision.vlm_session import build_session_factory, missing_packages  # noqa: E402
from tools.probe.vlm_bench import Sample, _num, _rate, measure, summarize  # noqa: E402
from tools.vlm_lora.dataset import (  # noqa: E402
    Entry,
    MissingQuestionError,
    file_sha256,
    question_for,
    read_jsonl,
)

FactoryBuilder = Callable[[Mapping[str, Any]], Callable[[], Any] | None]


def holdout_samples(entries: Sequence[Entry], keys: Sequence[str] | None) -> list[Sample]:
    """보류 줄을 `vlm_bench.Sample` 로. 장면은 묶음이고 프레임은 묶음 안 순서다."""
    frames: dict[tuple[str, str], int] = defaultdict(int)
    samples: list[Sample] = []
    for entry in entries:
        if entry.split != "holdout" or entry.answer not in ("yes", "no"):
            continue
        if keys is not None and entry.key not in keys:
            continue
        frame = frames[entry.key, entry.group]
        frames[entry.key, entry.group] += 1
        path = Path(entry.image)
        samples.append(Sample(entry.key, entry.answer, path, entry.image, entry.group, frame))
    return samples


def evaluate_model(
    model: str,
    samples: Sequence[Sample],
    vlm: Mapping[str, Any],
    *,
    build_factory: FactoryBuilder | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """모델 하나를 운용 설정에서 `model_id` 만 바꿔 올리고 잰다. (요약, 원자료)."""
    build = build_factory or build_session_factory
    factory = build({"vision": {"vlm": {**vlm, "model_id": model}}})
    if factory is None:
        raise RuntimeError(f"VLM 의존성이 없다 {list(missing_packages())}")
    questions = [question_for(key) for key in dict.fromkeys(s.key for s in samples)]
    print(f"적재 중… {model}", file=sys.stderr, flush=True)
    session = factory()
    try:
        records = measure(samples, session, budget_ms=int(vlm["budget_ms"]), questions=questions)
    finally:
        session.close()
    return summarize(records, 1), records


def compare(summaries: Mapping[str, dict[str, Any]]) -> str:
    """질문별로 모델들을 한 표에 나란히 둔다."""
    lines = [
        "| 질문 | 모델 | 예/아니오 | 적중률 | 오경보율 | 판독 불가 (예/아니오) | p50 ms | p95 ms |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    keys = dict.fromkeys(key for s in summaries.values() for key in s["questions"])
    for key in keys:
        for model, summary in summaries.items():
            entry = summary["questions"].get(key)
            if entry is None:
                continue
            f, u, t = entry["frames"], entry["unreadable"], entry["latency_ms"]
            lines.append(
                f"| `{key}` | {model} | {f['tp'] + f['fn']}/{f['fp'] + f['tn']} "
                f"| {_rate(f['recall'])} | {_rate(f['false_alarm'])} | {u['yes']}/{u['no']} "
                f"| {_num(t['p50'])} | {_num(t['p95'])} |"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("dataset", type=Path, help="dataset.py 가 만든 목록 JSONL")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--merged", type=Path, help="merge.py 의 출력 폴더")
    which.add_argument("--base-only", action="store_true", help="기본 모델만 잰다")
    parser.add_argument("--base", help="기본 모델 (기본: 운용 설정 vision.vlm.model_id)")
    parser.add_argument("--keys", nargs="+", help="잴 질문키 (기본: 목록의 전부)")
    parser.add_argument("--out", type=Path, help="원자료 JSON (이미지는 담지 않는다)")
    args = parser.parse_args(argv)

    vlm = dict(load_base_config()["vision"]["vlm"])
    models = [args.base or str(vlm["model_id"])]
    if args.merged is not None:
        if not args.merged.is_dir():
            print(f"{args.merged}: 병합 폴더가 없다", file=sys.stderr)
            return 2
        models.append(str(args.merged.resolve()))
    samples = holdout_samples(read_jsonl(args.dataset), args.keys)
    if not samples:
        print("보류 사진이 없다 — split=holdout 이고 정답이 있는 줄이 없다", file=sys.stderr)
        return 2
    try:  # 모델을 올리기 전에 질문 문장부터 본다
        for key in dict.fromkeys(sample.key for sample in samples):
            question_for(key)
    except MissingQuestionError as exc:
        print(exc, file=sys.stderr)
        return 2

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    summaries: dict[str, dict[str, Any]] = {}
    raw: dict[str, list[dict[str, Any]]] = {}
    started = time.monotonic()
    for model in models:
        try:
            summaries[model], raw[model] = evaluate_model(model, samples, vlm)
        except RuntimeError as exc:
            print(
                f"{exc} — ~/.venv-mechdog-vlm 의 python 으로 돌린다 (models/README.md ③)",
                file=sys.stderr,
            )
            return 1
    sha = file_sha256(args.dataset)
    print(
        f"# VLM LoRA 평가 — 보류 {len(samples)}장 · 목록 sha256 {sha[:12]} "
        f"· {round(time.monotonic() - started)}초\n"
    )
    print(compare(summaries))
    if args.out is not None:
        args.out.write_text(
            json.dumps(
                {
                    "dataset": str(args.dataset.resolve()),
                    "dataset_sha256": sha,
                    "models": {m: {"summary": summaries[m], "records": raw[m]} for m in models},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n원자료 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
