#!/usr/bin/env python3
"""문서 상대 링크 검사 — 문서를 옮기거나 고칠 때 깨진 링크를 남기지 않게 막는다.

문서를 `docs/internal/` 로 나누면서 상대 링크의 깊이가 바뀐다. 링크 하나가
어긋나도 렌더링된 화면은 멀쩡해 보여 사람 눈으로는 잘 안 잡히므로, git 이
추적하는 모든 `.md` 파일에서 상대 링크 대상이 실제로 존재하는지 기계로
확인한다.

검사 대상은 `[텍스트](경로)` 와 `[텍스트](경로#앵커)` 형태의 상대 경로 링크뿐이다.
http(s) · mailto · 순수 앵커(`#...`)는 건너뛰고, 앵커가 실제로 있는지는 보지
않는다 — **파일(또는 디렉터리) 존재만 본다.** 코드 블록(``` 안) 속 링크도
건너뛴다.

    python tools/check_doc_links.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from host.common.console import survive_encoding_errors  # noqa: E402

#: `[텍스트](경로)` 또는 `[텍스트](경로 "제목")` — 제목이 있으면 버린다.
_LINK = re.compile(r"\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def tracked_markdown_files() -> list[Path]:
    """git 이 추적하는 모든 `.md` 파일 (저장소 루트 기준 경로)."""
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if line]


def _skip(target: str) -> bool:
    """검사하지 않을 대상 — 외부 링크와 순수 앵커."""
    return (
        not target
        or target.startswith("#")
        or target.startswith("http://")
        or target.startswith("https://")
        or target.startswith("mailto:")
    )


def find_broken_links(path: Path) -> list[tuple[int, str]]:
    """한 문서에서 대상이 없는 상대 링크를 `(줄 번호, 원래 대상)` 목록으로 낸다."""
    broken: list[tuple[int, str]] = []
    in_code_block = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        for target in _LINK.findall(line):
            if _skip(target):
                continue
            file_part = target.split("#", 1)[0]
            if not (path.parent / file_part).exists():
                broken.append((lineno, target))
    return broken


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **인자 처리보다 앞이다** — cp949 콘솔에서 `--help` 조차 죽었다
    # (CONTRIBUTING 8절). 도움말은 `argparse` 가 stdout 에 쓴다.
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        prog="check_doc_links", description="문서 상대 링크가 실제 파일을 가리키는지 검사"
    )
    parser.parse_args(argv)

    failures = [
        f"{path.relative_to(ROOT)}:{lineno} → {target}"
        for path in tracked_markdown_files()
        for lineno, target in find_broken_links(path)
    ]

    if failures:
        for line in failures:
            print(line)
        print(f"{len(failures)}건의 깨진 링크")
        return 1

    print("문제 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
