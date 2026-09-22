"""가상 MES 읽기 전용 어댑터 (WBS 4.7.15 · FR-12.1~12.3 · ADR-34).

현장지원 모드가 운영값을 읽는 **유일한 문**이다. 여는 방식 하나에 DoD 네 항목이
걸려 있다.

⚠️ **쓰기 API 가 없는 것이 아니라, 연결 자체가 읽기 전용이다** (DoD ④). SQLite 를
`mode=ro` URI 로 열면 INSERT·UPDATE·DELETE 가 드라이버에서 거부된다. "쓰는 함수를
만들지 않았다" 는 약속은 다음 사람이 하나 추가하면 끝나지만, 연결이 읽기 전용이면
추가해도 동작하지 않는다. **지켜야 할 규칙을 코드가 대신 지키게 둔다.**

⚠️ **없는 DB 를 만들지 않는다.** `mode=ro` 는 파일이 없으면 열리지 않으며, 그것이
맞는 동작이다. 자동 생성하면 **빈 표를 최신 운영값으로 읽는 길**이 생기고, 그때
로봇은 *"생산 실적이 없습니다"* 라고 자신 있게 답한다.

⚠️ **표 이름과 조회 칸은 화이트리스트를 지난 것만 SQL 에 들어간다** (FR-12.2). 값은
전부 바인딩 파라미터다. 질문이 SQL 이 되는 길을 여기서 끊어 두어야, 뒤에 붙는
라우터(`4.7.16`)가 실수해도 데이터 계층에서 한 번 더 막힌다.

⚠️ **`assist` 모드는 이 모듈만으로 열리지 않는다.** `host/behavior/mission.py` 의
`REQUIRES` 는 `service` 와 `router` 도 요구하며 그 둘은 `4.7.16`·`4.7.17` 이다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from host.common.config import repo_path

__all__ = [
    "DEFAULT_DB",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SOURCE",
    "TABLES",
    "FactoryOps",
    "Reading",
    "StoreError",
]

#: 값의 출처. 답변과 로그에 그대로 실린다 (FR-12.3).
#:
#: ⚠️ **행이 아니라 어댑터가 가진다.** 같은 숫자라도 어디서 읽었는지는 읽는 쪽이
#: 아는 사실이며, 실제 MES API 로 바꾸는 날 바뀌는 것도 이 값 하나다 (ADR-34 재검토).
SOURCE = "demo-mes"

#: 스키마 판. `schema.sql` 의 `PRAGMA user_version` 과 같아야 한다.
#:
#: ⚠️ **DB 는 생성물이라 낡은 것이 남는다.** 판을 안 보면 스키마를 고친 뒤에도
#: 옛 DB 를 조용히 읽어 **없는 칸을 빈 값으로** 답하게 된다. 그래서 대조한다.
SCHEMA_VERSION = 1

SCHEMA = Path(__file__).with_name("schema.sql")

#: 런타임 DB. **생성물이므로 커밋하지 않는다** (DoD ① · `.gitignore`).
DEFAULT_DB = repo_path("data/factory_ops.db")

#: 읽을 수 있는 표와, 그 표에서 **조회 조건으로 받아 주는 칸**.
#:
#: ⚠️ **첫 칸은 기본키이며 정렬 기준이다.** 정렬을 고정해야 `4.7.17` 의 골든 시험이
#: 성립한다 — 순서가 흔들리면 같은 질문에 같은 답이 나온다고 말할 수 없다.
TABLES: dict[str, tuple[str, ...]] = {
    "production_status": ("line_id", "state"),
    "shipment_schedule": ("shipment_id", "customer", "deadline"),
    "work_schedule": ("task_id", "line_id", "status"),
    "equipment_status": ("equipment_id", "line_id", "state"),
}


class StoreError(RuntimeError):
    """운영 DB 를 읽을 수 없거나 허용되지 않은 조회.

    ⚠️ **부르는 쪽은 이것을 «값 없음» 으로 바꾸지 «추정» 으로 바꾸지 않는다**
    (FR-12.3). `4.7.17` 이 «최신 정보를 확인할 수 없습니다» 로 닫는다.
    """


@dataclass(frozen=True, slots=True)
class Reading:
    """조회 한 번의 결과. **값과 출처와 갱신 시각이 함께 다닌다** (FR-12.3)."""

    source: str
    #: 읽은 행 중 **가장 오래된** `updated_at`. 비었으면 빈 문자열이다.
    #:
    #: ⚠️ **가장 최신이 아니라 가장 오래된 값이다.** 조회 한 번을 한 값으로 요약할
    #: 때 최신 쪽을 고르면 낡은 줄이 숨는다. 이 값은 기록에 남기는 보수적인 요약이고,
    #: 답변에 넣을 행을 고르는 판정은 `service.mark_freshness` 가 행마다 따로 한다.
    updated_at: str
    rows: tuple[dict[str, Any], ...]


def _connect(db_path: Path) -> sqlite3.Connection:
    """읽기 전용으로 연다. **없으면 만들지 않고 실패한다.**"""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise StoreError(
            f"운영 DB 를 열 수 없다: {db_path} — `python tools/factory_ops_seed.py` 로 만든다"
        ) from exc
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise StoreError(
            f"스키마 판이 다르다 (DB={version} 코드={SCHEMA_VERSION}): {db_path}"
            " — `python tools/factory_ops_seed.py --force` 로 다시 만든다"
        )
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True, slots=True)
class FactoryOps:
    """가상 MES 어댑터.

    ⚠️ **연결을 들고 있지 않고 조회마다 열고 닫는다.** 런타임은 여러 스레드에서
    돌고 sqlite3 연결은 만든 스레드에 묶이므로, 하나를 오래 들고 있으면 그 규칙을
    따로 지켜야 한다. 질의는 분당 몇 건 수준이라 여는 비용이 문제가 되지 않는다.
    """

    db_path: Path = DEFAULT_DB
    source: str = SOURCE

    def read(self, table: str, **filters: Any) -> Reading:
        """표 하나를 읽는다.

        표 이름과 조회 칸은 `TABLES` 에 있는 것만 SQL 에 들어가고, 값은 전부 바인딩
        파라미터다. 그래서 문자열을 이어 붙이는 자리에 사용자 입력이 닿지 않는다.
        """
        columns = TABLES.get(table)
        if columns is None:
            raise StoreError(f"허용되지 않은 표: {table!r}")
        unknown = sorted(set(filters) - set(columns))
        if unknown:
            raise StoreError(f"{table}: 허용되지 않은 조회 칸 {unknown}")

        keys = tuple(filters)
        sql = f'SELECT * FROM "{table}"'  # 이름은 위 화이트리스트를 지난 것뿐이다
        if keys:
            sql += " WHERE " + " AND ".join(f'"{c}" = ?' for c in keys)
        sql += f' ORDER BY "{columns[0]}"'

        conn = _connect(self.db_path)
        try:
            rows = tuple(dict(r) for r in conn.execute(sql, [filters[k] for k in keys]))
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"{table}: 조회 실패 ({exc})") from exc
        finally:
            conn.close()
        oldest = min((r["updated_at"] for r in rows), key=datetime.fromisoformat, default="")
        return Reading(source=self.source, updated_at=oldest, rows=rows)
