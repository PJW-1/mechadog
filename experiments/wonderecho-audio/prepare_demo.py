"""Recreate the shared synthetic dataset as the voice DB used by the voice tools.

Run from this directory: python prepare_demo.py --output-dir .
Existing files are kept, including intentionally deleted roster entries. New DB
imports are transactional. No servers, models, network or hardware are started.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import db_transfer
import voice_rules
import voice_schema
from sqlite_admin import open_readonly

DEFAULT_BUNDLE = Path(__file__).with_name("demo") / "voice_demo.json"


def prepare(output_dir, bundle_path=DEFAULT_BUNDLE):
    bundle = db_transfer.validate(json.loads(Path(bundle_path).read_text(encoding="utf-8-sig")))
    if set(bundle["tables"]) != set(db_transfer.TABLES) or "rules" not in bundle:
        raise ValueError("음성3 테이블과 rules가 모두 있는 합성 묶음이 필요합니다")
    output = Path(output_dir).resolve()
    voice_db = output / "voice_data.db"
    # Check the target before making any change. Never leave legacy rules unused.
    if voice_db.exists():
        conn = open_readonly(voice_db)
        try:
            voice_schema.validate(conn)
            names = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if names & voice_rules.LEGACY.keys():
                raise ValueError("기존 8테이블 DB는 migrate_voice_db.py로 먼저 이전하세요")
            if not set(db_transfer.VOICE) <= names:
                raise ValueError("대상 파일에 필요한 테이블이 없습니다. 새 폴더에서 생성하세요")
        finally:
            conn.close()
    config = voice_rules.path_for(voice_db)
    if config.exists():
        voice_rules.read(config)
    output.mkdir(parents=True, exist_ok=True)
    if voice_db.exists():
        result = {"status": "kept existing file"}
    else:
        result = {"status": "created", **db_transfer.import_sqlite(bundle, voice_db)}
    return {
        "output_dir": str(output),
        "data_kind": bundle["data_kind"],
        "databases": {voice_db.name: result},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    args = ap.parse_args()
    try:
        print(json.dumps(prepare(args.output_dir, args.bundle), ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        ap.exit(
            1,
            f"[prepare-demo] {type(exc).__name__}: DB 생성 실패. 입력과 대상 폴더를 확인하세요.\n",
        )


if __name__ == "__main__":
    main()
