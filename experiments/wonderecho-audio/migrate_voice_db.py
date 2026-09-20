"""Explicit, backed-up migration from eight voice tables to three + JSON rules.

Stop processes editing the DB/config before running. --check only reads.
Legacy phrases_custom.json is imported once and retained as .migrated.bak.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import voice_rules
import voice_store
from sqlite_admin import backup_database, open_readonly


def _plan(conn, db_path):
    conn.row_factory = sqlite3.Row
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = {}
    for table, cols in voice_rules.LEGACY.items():
        if table in names:
            legacy[table] = [
                dict(r)
                for r in conn.execute(f'SELECT {",".join(cols)} FROM "{table}" ORDER BY rowid')
            ]
    rules = voice_rules.from_legacy(legacy) if legacy else None
    path = voice_rules.path_for(db_path)
    if rules and path.exists() and voice_rules.read(path) != rules:
        raise ValueError("기존 JSON 규칙과 DB 규칙이 다릅니다. 덮어쓰지 않고 중단합니다")
    custom = Path(db_path).with_name("phrases_custom.json")
    phrases = []
    if custom.exists():
        data = json.loads(custom.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("추가 문구 JSON은 객체여야 합니다")
        for category, texts in data.items():
            if not isinstance(texts, list):
                raise ValueError("추가 문구 JSON 값은 목록이어야 합니다")
            for text in texts:
                voice_store.validate_phrase(category, text)
                phrases.append((category, text))
    return legacy, rules, phrases, custom


def migrate(db_path, *, check=False):
    db_path = Path(db_path).resolve()
    conn = (
        open_readonly(db_path)
        if check
        else sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True)
    )
    try:
        conn.execute("BEGIN" if check else "BEGIN IMMEDIATE")
        legacy, rules, phrases, custom = _plan(conn, db_path)
        result = {
            "legacy_rows": {t: len(rows) for t, rows in legacy.items()},
            "custom_phrases": len(phrases),
            "rules_file": str(voice_rules.path_for(db_path)) if legacy else None,
            "backup": None,
            "changed": False,
        }
        if check or not (legacy or custom.exists()):
            return result
        # A separate read connection can back up while this connection holds the write lock.
        result["backup"] = str(backup_database(db_path))
        for statement in voice_store.SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.executemany("INSERT OR IGNORE INTO phrases VALUES (?,?)", phrases)
        if rules is not None:
            voice_rules.save(voice_rules.path_for(db_path), rules)
        for table in legacy:
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        result["changed"] = True
        # Rename after commit: even an interrupted rename can only cause an idempotent reimport.
        if custom.exists():
            archive = custom.with_name(custom.name + ".migrated.bak")
            if archive.exists():
                archive = custom.with_name(custom.name + f".{time.time_ns()}.migrated.bak")
            custom.rename(archive)
            result["custom_archive"] = str(archive)
        return result
    finally:
        conn.close()  # rollback on failure; JSON (if already saved) matches the old DB behavior


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=voice_store.DEFAULT_DB)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    try:
        result = migrate(args.db.resolve(), check=args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        ap.exit(
            1, f"[migrate] {type(exc).__name__}: 이전 실패, 원본/백업과 규칙 충돌을 확인하세요.\n"
        )


if __name__ == "__main__":
    main()
