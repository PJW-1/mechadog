"""가상 MES 런타임 DB 를 합성 자료로 만든다 (WBS 4.7.15).

    python tools/factory_ops_seed.py            # 없을 때만 만든다
    python tools/factory_ops_seed.py --force    # 지우고 다시 만든다
    python tools/factory_ops_seed.py --db data/other.db

정본은 `host/factory_ops/schema.sql` 과 `seed.json` 이고 DB 는 생성물이다. 그래서
DB 파일은 커밋하지 않으며, 스키마나 자료를 고친 사람은 이 도구를 다시 돌린다.

⚠️ **쓰기는 이 도구에만 있다.** 런타임이 읽는 `host/factory_ops/store.py` 는 DB 를
읽기 전용으로 열며 없는 DB 를 만들지도 않는다 (FR-12 · ADR-34).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.factory_ops.seed import build  # noqa: E402
from host.factory_ops.store import DEFAULT_DB, TABLES, FactoryOps, StoreError  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="가상 MES 합성 DB 생성")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"기본값 {DEFAULT_DB}")
    parser.add_argument("--force", action="store_true", help="이미 있으면 지우고 다시 만든다")
    args = parser.parse_args(argv)

    try:
        path = build(args.db, force=args.force)
    except StoreError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 1

    store = FactoryOps(db_path=path)
    print(f"만들었다: {path}")
    for table in TABLES:
        reading = store.read(table)
        print(f"  {table:<18} {len(reading.rows):>2}행  updated_at={reading.updated_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
