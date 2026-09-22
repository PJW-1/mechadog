"""합성 운영 데이터로 런타임 DB 를 만든다 (WBS 4.7.15 · FR-12.5).

⚠️ **응용 경로는 이 모듈을 부르지 않는다** (DoD ④). 쓰는 코드는 여기에만 있고
`store.py` 는 읽기 전용으로만 연다. import 하는 곳은 도구(`tools/factory_ops_seed.py`)와
시험뿐이며, 이 경계가 곧 *"쓰기·삭제 API 가 없다"* 의 구현이다.

⚠️ **날짜는 만드는 날 기준으로 푼다.** `seed.json` 은 `<칸이름>_in_days` 로 상대값을
적고 여기서 절대 날짜로 바꾼다. 절대 날짜를 파일에 적어 두면 며칠 뒤 다시 만들었을
때 «어제 납품했어야 할 일정» 이 그대로 남아 시연에서 그것을 설명해야 한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host.factory_ops.store import DEFAULT_DB, SCHEMA, TABLES, StoreError

__all__ = ["KST", "SEED", "build"]

KST = timezone(timedelta(hours=9))

SEED = Path(__file__).with_name("seed.json")

#: 상대 날짜를 적는 열쇠말. `deadline_in_days: 3` 은 `deadline` 칸이 된다.
_IN_DAYS = "_in_days"


def _resolve(row: dict[str, Any], today: Any, now: str) -> dict[str, Any]:
    """상대 날짜를 풀고 `updated_at` 을 붙인다."""
    resolved: dict[str, Any] = {}
    for key, value in row.items():
        if key.endswith(_IN_DAYS):
            resolved[key[: -len(_IN_DAYS)]] = (today + timedelta(days=value)).isoformat()
        else:
            resolved[key] = value
    resolved["updated_at"] = now
    return resolved


def build(db_path: Path = DEFAULT_DB, *, now: datetime | None = None, force: bool = False) -> Path:
    """`schema.sql` + `seed.json` 으로 DB 를 만든다. 만든 경로를 돌려준다.

    이미 있으면 `force` 없이는 거부한다. 생성물이라 다시 만들어도 잃을 것이 없지만,
    **지우는 일은 시키는 사람이 말해야 한다.**
    """
    db_path = Path(db_path)
    if db_path.exists():
        if not force:
            raise StoreError(f"이미 있다: {db_path} — 다시 만들려면 --force 를 준다")
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    stamped = now or datetime.now(KST)
    when = stamped.isoformat(timespec="seconds")
    today = stamped.date()
    seed = json.loads(SEED.read_text(encoding="utf-8"))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for table in TABLES:
            rows = seed.get(table)
            if not rows:
                raise StoreError(f"{SEED.name}: {table} 자료가 없다")
            for raw in rows:
                row = _resolve(raw, today, when)
                names = ", ".join(f'"{c}"' for c in row)
                marks = ", ".join("?" for _ in row)
                # 표·칸 이름은 `TABLES` 와 `seed.json` 에서만 오고 값은 전부 바인딩한다.
                conn.execute(
                    f'INSERT INTO "{table}" ({names}) VALUES ({marks})', list(row.values())
                )
        conn.commit()
    finally:
        conn.close()
    return db_path
