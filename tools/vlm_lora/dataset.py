"""LoRA 데이터 목록 — 두 형식의 사진을 한 JSONL 로 모으고 학습/보류로 나눈다.

    python tools/vlm_lora/dataset.py --staged <연출폴더> --auto <수집폴더> --out list.jsonl
    python tools/vlm_lora/dataset.py ... --holdout-date 20261004      # 보류 날짜를 지정
    python tools/vlm_lora/dataset.py ... --holdout-group staged/상자   # 보류 묶음을 지정

⚠️ **사진을 저장소에 커밋하지 않는다** — 얼굴이 찍힐 수 있다. 폴더와 목록은 저장소 밖에 둔다.

입력

    연출 촬영 (`--staged`)   `<root>/<질문키>/<yes|no>/<장면>_NN.jpg` — `vlm_bench.py capture` 형식
    자동 수집 (`--auto`)     `<root>/<YYYYMMDD>/<blocked|clear>/<파일>.jpg` + 날짜 폴더의
                             `manifest.jsonl` (줄마다 `file`·`label` 등). `file` 은 날짜 폴더
                             기준 상대 경로이거나, 라벨 폴더 안의 파일 이름이다.

자동 수집 라벨 규칙 (`AUTO_RULES`)

    LiDAR 의 `blocked`/`clear` 는 «막혔나» 만 말하고 «무엇이 막았나» 는 말하지 않는다.

    | 라벨     | blocked_path | blocked_by_fallen          |
    | clear    | 아니오       | 아니오 (막힘이 없으니 확실) |
    | blocked  | 예           | 미상 → `review`            |

    미상은 목록에 `answer: null`·`split: review` 로 남기고 학습·평가에서 뺀다. 사람이 보고
    정하면 연출 폴더 형식(`blocked_by_fallen/<yes|no>/`)으로 **복사해** 넣는다 — 옮기면
    manifest 가 가리키는 사진이 없어져 멈춘다. 다른 질문키(`person_down` 등)는 LiDAR 라벨로
    알 수 없어 목록에 넣지 않는다.

분할 — 다른 날·다른 배치로 보류한다 (ADR-43 «재검토»)

    묶음(`group`)은 연출 사진이면 `staged/<장면>`, 자동 수집이면 `auto/<날짜>` 다. 묶음은
    통째로 한쪽에만 간다. 보류는 `--holdout-date`·`--holdout-group` 으로 지정하고, 둘 다
    없으면 가장 늦은 날짜를 보류한다(날짜가 하나뿐이면 멈춘다). 보류 날짜가 하나라도 섞인
    묶음은 통째로 보류로 간다 — 그래서 학습에는 보류 날짜의 사진이 없다. 내용(sha256)이
    같은 사진이 든 묶음들은 하나로 이어 함께 움직인다(복사본 누수 방지).

출력 (JSONL 한 줄 = 사진 하나 × 질문 하나)

    image(절대 경로)·key·answer(yes|no|null)·source(staged|auto)·date(YYYYMMDD)·group·
    origin(자동 수집 라벨 또는 연출 폴더의 yes/no)·split(train|holdout|review)·sha256(사진 내용)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.console import survive_encoding_errors  # noqa: E402
from host.vision import vlm_reader  # noqa: E402
from host.vision.vlm_reader import Question  # noqa: E402
from tools.probe.vlm_bench import IMAGE_SUFFIXES, LayoutError, collect  # noqa: E402

AUTO_LABELS = ("blocked", "clear")
#: 자동 수집 라벨 → 질문키별 정답. `None` 은 «미상»(사람 확인 전에는 학습·평가에서 뺀다).
AUTO_RULES: dict[str, dict[str, str | None]] = {
    "clear": {"blocked_path": "no", "blocked_by_fallen": "no"},
    "blocked": {"blocked_path": "yes", "blocked_by_fallen": None},
}
_DATE = re.compile(r"^\d{8}$")
SPLITS = ("train", "holdout", "review")


@dataclass(frozen=True, slots=True)
class Entry:
    """사진 하나 × 질문 하나."""

    image: str
    key: str
    #: `yes`·`no`, 또는 미상(`None`)
    answer: str | None
    source: str
    date: str
    #: 분할 단위. 같은 묶음은 학습·보류 중 한쪽에만 간다.
    group: str
    origin: str
    split: str = ""
    #: 사진 내용의 sha256. 같은 사진이 다른 경로(복사본)로 들어와도 같은 사진으로 본다.
    sha256: str = ""


class ListError(ValueError):
    """목록 JSONL 한 줄을 Entry 로 읽지 못했다 — 메시지에 `파일:줄` 이 붙는다."""


class MissingQuestionError(LookupError):
    """질문키가 운용 질문 셋(`vlm_reader.QUESTIONS`)에 없다."""


def question_keys() -> tuple[str, ...]:
    """지금 운용 질문 셋의 키. 부를 때마다 `vlm_reader.QUESTIONS` 를 읽는다."""
    return tuple(question.key for question in vlm_reader.QUESTIONS)


def question_for(key: str) -> Question:
    """질문키의 질문 — 문장은 이 도구에 복사해 두지 않고 운용 질문 셋에서만 읽는다.

    운용과 다른 문장으로 학습하면 안 되므로, 없으면 지어내지 않고 `MissingQuestionError` 로 멈춘다.
    """
    for question in vlm_reader.QUESTIONS:
        if question.key == key:
            return question
    raise MissingQuestionError(
        f"질문 키 {key} 이 vlm_reader.QUESTIONS 에 없다 (있는 키: {', '.join(question_keys())})"
    )


def auto_answer(label: str, key: str) -> str | None:
    """자동 수집 라벨로 본 질문키의 정답. 미상이면 `None`."""
    return AUTO_RULES[label][key]


def _content_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_date(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def read_staged(root: Path) -> list[Entry]:
    """연출 촬영 폴더. 날짜는 파일 수정 시각(찍은 날)이고 묶음은 장면 이름이다.

    같은 사진을 두 질문 폴더에 넣어도 장면 이름이 같으니 같은 묶음이 된다.
    """
    return [
        Entry(
            image=str(sample.path.resolve()),
            key=sample.key,
            answer=sample.label,
            source="staged",
            date=_file_date(sample.path),
            group=f"staged/{sample.scene}",
            origin=sample.label,
            sha256=_content_sha256(sample.path),
        )
        for sample in collect(root, keys=question_keys())
    ]


def _resolve(day: Path, file: str, label: str) -> Path:
    path = day / file
    if not path.exists() and "/" not in file and "\\" not in file:
        path = day / label / file
    return path


def read_auto(root: Path) -> list[Entry]:
    """자동 수집 폴더. 틀린 곳이 있으면 모두 모아 `LayoutError`."""
    if not root.is_dir():
        raise LayoutError([f"{root}: 폴더가 없다"])
    problems: list[str] = []
    entries: list[Entry] = []
    for day in sorted(root.iterdir()):
        if not day.is_dir() or not _DATE.match(day.name):
            problems.append(f"{day.name}: 날짜(YYYYMMDD) 폴더가 아니다")
            continue
        manifest = day / "manifest.jsonl"
        if not manifest.is_file():
            problems.append(f"{day.name}: manifest.jsonl 이 없다")
            continue
        lines = manifest.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            where = f"{day.name}/manifest.jsonl:{number}"
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{where}: JSON 이 아니다")
                continue
            if not isinstance(record, dict):
                problems.append(f"{where}: JSON 객체가 아니다")
                continue
            label, file = record.get("label"), str(record.get("file", ""))
            if label not in AUTO_LABELS:
                problems.append(f"{where}: 라벨이 blocked·clear 가 아니다 ({label!r})")
                continue
            path = _resolve(day, file, label)
            if not file or not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                problems.append(f"{where}: {file}: 사진이 없다")
                continue
            folder = path.parent.name
            if folder in AUTO_LABELS and folder != label:
                problems.append(f"{where}: {file}: 폴더({folder})와 라벨({label})이 다르다")
                continue
            sha = _content_sha256(path)
            for key, answer in AUTO_RULES[label].items():
                entries.append(
                    Entry(
                        image=str(path.resolve()),
                        key=key,
                        answer=answer,
                        source="auto",
                        date=day.name,
                        group=f"auto/{day.name}",
                        origin=label,
                        sha256=sha,
                    )
                )
    if problems:
        raise LayoutError(problems)
    return entries


def check_no_leak(entries: Iterable[Entry]) -> None:
    """학습과 보류에 같은 묶음이나 같은 사진이 있으면 `ValueError`.

    사진은 경로가 아니라 내용(sha256)으로 견준다 — 복사본은 경로가 달라도 같은 사진이다.
    """
    sides: dict[str, dict[str, set[str]]] = {
        "group": {"train": set(), "holdout": set()},
        "image": {"train": set(), "holdout": set()},
    }
    for entry in entries:
        if entry.split in ("train", "holdout"):
            sides["group"][entry.split].add(entry.group)
            sides["image"][entry.split].add(entry.sha256 or entry.image)
    for kind, name in (("group", "묶음"), ("image", "사진")):
        both = sides[kind]["train"] & sides[kind]["holdout"]
        if both:
            raise ValueError(f"학습과 보류에 같은 {name}이 있다: {sorted(both)[:5]}")


def _linked_groups(entries: Sequence[Entry]) -> dict[str, str]:
    """같은 사진(sha256)을 가진 묶음들을 하나로 잇는다(union-find). 묶음 → 대표 묶음."""
    parent: dict[str, str] = {}

    def find(group: str) -> str:
        parent.setdefault(group, group)
        while parent[group] != group:
            parent[group] = parent[parent[group]]
            group = parent[group]
        return group

    first: dict[str, str] = {}
    for entry in entries:
        find(entry.group)
        if not entry.sha256:
            continue
        if entry.sha256 in first:
            parent[find(entry.group)] = find(first[entry.sha256])
        else:
            first[entry.sha256] = entry.group
    return {group: find(group) for group in parent}


def split_entries(
    entries: Sequence[Entry],
    *,
    holdout_dates: Sequence[str] = (),
    holdout_groups: Sequence[str] = (),
) -> list[Entry]:
    """묶음 단위로 학습/보류를 나눈다. 정답 미상은 `review` 로 뺀다.

    같은 사진이 든 묶음들(예: 자동 수집 원본과 사람이 확인해 연출 폴더로 복사한 사본)은
    하나로 이어 통째로 한쪽에 보낸다.
    """
    labelled = [entry for entry in entries if entry.answer is not None]
    dates = {entry.date for entry in labelled}
    groups = {entry.group for entry in labelled}
    if unknown := sorted(set(holdout_dates) - dates):
        raise ValueError(f"목록에 없는 날짜다: {unknown} (있는 날짜: {sorted(dates)})")
    if unknown := sorted(set(holdout_groups) - groups):
        raise ValueError(f"목록에 없는 묶음이다: {unknown}")
    chosen_dates = set(holdout_dates)
    if not holdout_dates and not holdout_groups:
        if len(dates) < 2:
            raise ValueError(
                f"날짜가 하나뿐이다 {sorted(dates)} — 다른 날 보류를 만들 수 없으니 "
                "--holdout-group 으로 보류 묶음을 고른다"
            )
        chosen_dates = {max(dates)}
    held = set(holdout_groups) | {e.group for e in labelled if e.date in chosen_dates}
    root = _linked_groups(entries)
    held_roots = {root[group] for group in held}
    held = {group for group in root if root[group] in held_roots}

    result = [
        replace(
            entry,
            split="review"
            if entry.answer is None
            else ("holdout" if entry.group in held else "train"),
        )
        for entry in entries
    ]
    counts = Counter(entry.split for entry in result)
    for side, name in (("train", "학습"), ("holdout", "보류")):
        if not counts[side]:
            raise ValueError(f"{name} 세트가 비었다 — 보류 지정을 바꾼다")
    check_no_leak(result)
    return result


def write_jsonl(entries: Iterable[Entry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for entry in entries:
            out.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[Entry]:
    """목록을 읽는다. 깨진 줄은 `ListError(파일:줄: 까닭)` 로 멈춘다 — 손으로 고친 목록을 위한 것."""
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ListError(f"{where}: JSON 이 아니다 ({error.msg})") from None
        if not isinstance(record, dict):
            raise ListError(f"{where}: JSON 객체가 아니다")
        try:
            entries.append(Entry(**record))
        except TypeError as error:
            raise ListError(f"{where}: 목록 줄이 아니다 ({error})") from None
    return entries


def file_sha256(path: Path) -> str:
    """목록 파일의 해시 — 학습 설정과 병합 기록에 남겨 어떤 목록으로 만들었는지 잇는다."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summary_lines(entries: Sequence[Entry]) -> list[str]:
    counts = Counter((e.split, e.key, e.answer) for e in entries)
    lines = ["| 분할 | 질문 | 예 | 아니오 | 미상 |", "| :--- | :--- | ---: | ---: | ---: |"]
    for split in SPLITS:
        for key in dict.fromkeys(e.key for e in entries):
            row = [counts[split, key, answer] for answer in ("yes", "no", None)]
            if any(row):
                lines.append(f"| {split} | `{key}` | {row[0]} | {row[1]} | {row[2]} |")
    holdout = sorted({e.group for e in entries if e.split == "holdout"})
    lines += ["", f"보류 묶음: {', '.join(holdout)}"]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--staged", type=Path, action="append", default=[], help="연출 촬영 폴더")
    parser.add_argument("--auto", type=Path, action="append", default=[], help="자동 수집 폴더")
    parser.add_argument("--out", type=Path, required=True, help="목록 JSONL (저장소 밖)")
    parser.add_argument("--holdout-date", action="append", default=[], help="보류할 날짜")
    parser.add_argument("--holdout-group", action="append", default=[], help="보류할 묶음")
    args = parser.parse_args(argv)
    if not args.staged and not args.auto:
        parser.error("--staged 나 --auto 가 하나는 있어야 한다")

    try:
        entries = [e for root in args.staged for e in read_staged(root)]
        entries += [e for root in args.auto for e in read_auto(root)]
        entries = split_entries(
            entries, holdout_dates=args.holdout_date, holdout_groups=args.holdout_group
        )
    except LayoutError as exc:
        print("폴더 구조가 틀렸다", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"분할할 수 없다: {exc}", file=sys.stderr)
        return 2
    write_jsonl(entries, args.out)
    print("\n".join(summary_lines(entries)))
    print(f"\n목록 → {args.out} · sha256 {file_sha256(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
