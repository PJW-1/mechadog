"""블랙박스 폴더에서 사건 이력 DB 를 다시 만든다 — 색인 복구·기존 기록 채우기 (ADR-46).

    python tools/ops/history_import.py --device <unit-id> [--blackbox DIR] [--db PATH]

`<epoch_ms>_<event>/meta.json` 하나가 사건 한 건이다. 몇 번을 돌려도 같다 — 이미 있는 사건은
건드리지 않는다(검토 기록 보존). 기체 이름은 폴더에 없어 `--device` 로 준 것을 붙인다.
순찰 판(`mission_runs`)은 블랙박스에 없으므로 만들지 않는다.

종료 코드: 0 정상 · 1 넣지 못한 사건이 있음 · 2 인자·설정 오류.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.common.config import ConfigError, load_base_config, repo_path  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.history import HistoryStore, incident_from_meta  # noqa: E402

#: 블랙박스 폴더 이름 — `<epoch_ms>_<event>[_<n>]` (`EventBlackbox._new_entry_dir`).
_ENTRY_NAME = re.compile(r"^\d+_.+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="블랙박스 → 사건 이력 DB 가져오기")
    parser.add_argument("--device", required=True, help="사건을 붙일 기체 ID")
    parser.add_argument("--blackbox", default=None, help="기본은 logging.blackbox_dir")
    parser.add_argument("--db", default=None, help="기본은 logging.history_db")
    return parser


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    try:
        logging_config = load_base_config().get("logging") or {}
    except ConfigError as exc:
        print(f"[HistoryImport] 설정 오류: {exc}", file=sys.stderr)
        return 2
    blackbox = args.blackbox or logging_config.get("blackbox_dir")
    db = args.db or logging_config.get("history_db")
    if not db:
        print("[HistoryImport] logging.history_db 가 비어 있다; --db 를 준다", file=sys.stderr)
        return 2
    root = repo_path(blackbox) if blackbox else None
    if root is None or not root.is_dir():
        print(f"[HistoryImport] 블랙박스 폴더가 없다: {root}", file=sys.stderr)
        return 2

    counts = {"inserted": 0, "existing": 0, "skipped": 0, "failed": 0}
    try:
        store = HistoryStore(repo_path(db))
    except (sqlite3.Error, OSError) as exc:
        print(f"[HistoryImport] DB 를 열지 못했다: {exc}", file=sys.stderr)
        return 2
    try:
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or not _ENTRY_NAME.match(folder.name):
                counts["skipped"] += 1
                continue
            try:
                meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
                row = incident_from_meta(
                    folder.name,
                    meta,
                    robot_id=args.device,
                    snapshot_name="snapshot.jpg" if (folder / "snapshot.jpg").is_file() else None,
                )
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                counts["skipped"] += 1
                continue
            if store.incident(folder.name) is not None:
                counts["existing"] += 1
            elif store.record_incident(row):
                counts["inserted"] += 1
            else:
                counts["failed"] += 1
    finally:
        store.close()
    print(
        f"[HistoryImport] inserted {counts['inserted']} / existing {counts['existing']} / "
        f"skipped {counts['skipped']} / failed {counts['failed']}"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
