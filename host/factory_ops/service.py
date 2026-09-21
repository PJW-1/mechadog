"""운영 데이터 원본 + 신선도 계약 (WBS 4.7.15 · 4.7.17 · ADR-34).

**로컬 SQLite 를 읽기 전용으로만 연다.** 실제 MES 가 생기면 `_fetch` 한 곳을 실
API 어댑터로 바꾸고 `source`·`updated_at`·TTL·읽기 전용 계약은 그대로 둔다
(ADR-34 「재검토」).

⚠️ **운용 경로에 쓰기가 없다** (`4.7.15` DoD ④). 연결을 `mode=ro` 로 열므로
실수로 `INSERT` 를 적어도 sqlite 가 거절한다 — 규약을 주석이 아니라 **연결
플래그로** 지킨다. 자료를 넣는 길은 `seed()` 하나이고 그것은 도구 경로다.

⚠️ **런타임 DB 는 생성물이다** (`4.7.15` DoD ①). 버전 관리하는 것은 `schema.sql`
과 `seed.json` 뿐이고 `.db` 는 `.gitignore` 다. 파일이 없으면 만들라고 말하고
멈춘다 — 빈 DB 로 «자료 없음» 을 답하면 **고장과 «오늘 일정 없음» 이 같은
문장이 된다.**

⚠️ **신선도 판정은 답변이 아니라 여기서 한다** (`4.7.17` DoD ②③). 행마다
`fresh` 를 붙이고, TTL 을 넘긴 값은 **추정 없이 실패로 돌린다** (ADR-34 운용규칙
4). 그래서 *"모르면 모른다고 한다"* 가 문구가 아니라 자료 구조에 있다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host.common.config import ConfigError, load_base_config

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEED_PATH = Path(__file__).with_name("seed.json")

KST = timezone(timedelta(hours=9))

#: 조회 결과에 붙는 출처 이름. **합성 자료임을 답변에서 숨기지 않는다.**
SOURCE = "데모 MES"

#: `schema.sql` 의 네 테이블 (ADR-34 「데이터 범위」). 이름이 `seed.json` 의 키다.
TABLES: tuple[str, ...] = (
    "production_status",
    "shipment_schedule",
    "work_schedule",
    "equipment_status",
)

#: 조회군 → 테이블. 라우터가 이 이름으로 부른다 (`4.7.16`).
ENDPOINTS: Mapping[str, str] = {
    "production": "production_status",
    "shipments": "shipment_schedule",
    "schedule": "work_schedule",
    "equipment": "equipment_status",
}

#: TTL 을 적지 않았을 때 쓰는 값. **설정이 정본이고 이것은 마지막 안전망이다.**
DEFAULT_TTL_S = 6 * 3600


class SourceError(ConfigError):
    """데이터 원본을 열 수 없음 — DB 파일 없음·스키마 깨짐·sqlite 오류.

    ⚠️ **`ConfigError` 를 물려받는다.** `mission.ModeError` 와 같은 이유다 —
    기동을 거부하는 뜻이 *"이 설정으로는 켤 수 없다"* 로 같아서 `runtime` 의
    설정 오류 경로가 새 `except` 절 없이 이것도 잡는다.
    """


class UnknownLineError(SourceError):
    """등록되지 않은 라인 — 조회 실패이지 «기록 없음» 이 아니다."""

    def __init__(self, line: str, valid: Sequence[str]) -> None:
        self.line = line
        self.valid = tuple(valid)
        super().__init__(f"등록되지 않은 라인: {line} (등록: {', '.join(valid) or '없음'})")


def now() -> datetime:
    return datetime.now(KST)


def _resolve(value: Any, at: datetime) -> Any:
    """`seed.json` 의 시각 자리표시자를 푼다.

    `"@now"` 는 적재 시각, `"@+N"` 은 적재일 +N일. **날짜를 리터럴로 적지 않는
    이유** — 어느 날 seed 해도 「내일 납품」이 내일이어야 한다.
    """
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    if value == "@now":
        return at.isoformat(timespec="seconds")
    return (at.date() + timedelta(days=int(value[1:]))).isoformat()


def seed(db_path: Path, *, at: datetime | None = None) -> bool:
    """빈 DB 에 스키마와 합성 자료를 넣는다. 이미 자료가 있으면 건드리지 않는다.

    돌려주는 값은 *"넣었는가"* 다. **덮어쓰기를 하지 않는다** — 운용 중인 DB 를
    도구 한 번으로 날리는 길을 두지 않기 위해서다.
    """
    at = at or now()
    rows = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute("BEGIN IMMEDIATE")
        if any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES):
            conn.rollback()
            return False
        for table in TABLES:
            values = [tuple(_resolve(v, at) for v in row) for row in rows[table]]
            marks = ",".join("?" * len(values[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", values)
        conn.commit()
        return True
    finally:
        conn.close()


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise SourceError(
            f"운영 데이터 DB 없음: {db_path}\n"
            f"`python -m host.factory_ops.service --seed` 로 합성 자료를 만든다."
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch(db_path: Path, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
    conn = _open_readonly(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args))]
    except sqlite3.Error as exc:  # 스키마가 없거나 깨진 DB
        raise SourceError(f"운영 데이터 조회 실패: {exc}") from exc
    finally:
        conn.close()


def mark_freshness(
    rows: list[dict[str, Any]], ttl_s: float, *, at: datetime | None = None
) -> list[dict[str, Any]]:
    """행마다 `source`·`fresh` 를 붙인다 (`4.7.17` DoD ②).

    ⚠️ **미래 시각도 오래된 것으로 친다.** 시계가 어긋난 원본을 «방금 자료» 로
    읽으면 TTL 이 아무것도 막지 못한다.
    """
    at = at or now()
    for row in rows:
        row["source"] = SOURCE
        try:
            age = (at - datetime.fromisoformat(str(row["updated_at"]))).total_seconds()
            row["fresh"] = 0 <= age <= ttl_s
        except (KeyError, TypeError, ValueError):
            row["fresh"] = False
    return rows


class Service:
    """읽기 전용 조회 + 신선도. **라우터가 쓰는 provider 가 이것이다.**"""

    def __init__(self, db_path: Path, ttl_s: Mapping[str, float] | None = None) -> None:
        self.db_path = Path(db_path)
        self.ttl_s = dict(ttl_s or {})

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> Service:
        """`config.yaml` 의 `factory_ops` 절로 만든다.

        ⚠️ **절을 `REQUIRED_SECTIONS` 에 넣지 않았다.** 넣으면 `assist` 를 쓰지
        않는 경비 운용까지 기동을 막는다 — `lidar` 절과 같은 판단이다.
        """
        config = config if config is not None else load_base_config()
        section = config.get("factory_ops")
        if not isinstance(section, dict):
            raise SourceError("config.yaml 에 factory_ops 절이 없음 — ADR-34 는 이 절을 요구한다")
        db_path = section.get("db_path")
        if not isinstance(db_path, str) or not db_path.strip():
            raise SourceError("factory_ops.db_path 는 비어 있지 않은 문자열이어야 함")
        path = Path(db_path)
        ttl = section.get("ttl_s")
        return cls(
            path if path.is_absolute() else ROOT / path, ttl if isinstance(ttl, dict) else {}
        )

    def ttl_for(self, endpoint: str) -> float:
        """항목별 TTL (`4.7.17` DoD ①). 적히지 않은 항목은 기본값."""
        try:
            return float(self.ttl_s[endpoint])
        except (KeyError, TypeError, ValueError):
            return DEFAULT_TTL_S

    def known_lines(self) -> tuple[str, ...]:
        """등록된 라인 ID.

        **«없는 라인» 과 «기록 0건» 을 구분하기 위해 있다.** 둘을 같은 답으로
        만들면 «D라인 작업 있나요» 에 *"없습니다"* 가 나오고, 묻는 사람은 라인이
        한가한 줄 안다.
        """
        seen: set[str] = set()
        for table in ("production_status", "work_schedule", "equipment_status"):
            seen.update(
                str(r["line_id"]) for r in _fetch(self.db_path, f"SELECT line_id FROM {table}")
            )
        return tuple(sorted(seen))

    def query(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """조회군 → 행 목록. 모르는 조회군은 `SourceError` 다.

        ⚠️ **파라미터는 화이트리스트로만 바인딩한다** (`4.7.16` DoD). 라인 ID 와
        일수, 두 가지뿐이고 둘 다 바인딩 파라미터로 들어간다 — 질의 문자열이
        SQL 에 섞이는 길이 없다.
        """
        params = dict(params or {})
        table = ENDPOINTS.get(endpoint)
        if table is None:
            raise SourceError(f"모르는 조회군: {endpoint}")
        line = str(params["line"]).upper() if params.get("line") else None
        if line and line not in self.known_lines():
            raise UnknownLineError(line, self.known_lines())

        args: list[Any] = []
        if endpoint == "shipments":
            days = int(params.get("days", 7))
            args = [((at or now()).date() + timedelta(days=days)).isoformat()]
            sql = f"SELECT * FROM {table} WHERE deadline <= ? ORDER BY deadline"
        elif endpoint == "schedule":
            sql = f"SELECT * FROM {table} WHERE status != 'done'"
            if line:
                sql += " AND line_id = ?"
                args.append(line)
            sql += " ORDER BY priority"
        else:  # production · equipment — 라인 필터만 다르다
            sql = f"SELECT * FROM {table}"
            if line:
                sql += " WHERE line_id = ?"
                args.append(line)
            sql += " ORDER BY line_id" if endpoint == "production" else " ORDER BY equipment"

        rows = _fetch(self.db_path, sql, args)
        if endpoint == "production":
            for row in rows:
                row["remaining_quantity"] = max(
                    0, int(row["target_quantity"]) - int(row["completed_quantity"])
                )
        return mark_freshness(rows, self.ttl_for(endpoint), at=at)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m host.factory_ops.service --seed` — 합성 DB 를 만든다."""
    import argparse
    import sys

    # ⚠️ 한국어 Windows 콘솔은 cp949 라 `—` 에서 죽는다 — `tools/fetch_models.py`
    # 가 첫 실행에서 실제로 그렇게 죽었다. 글자가 물음표로 나오는 편이 낫다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="factory_ops.service", description="가상 MES 합성 DB")
    parser.add_argument("--seed", action="store_true", help="빈 DB 에 합성 자료를 넣는다")
    args = parser.parse_args(argv)
    if not args.seed:
        parser.error("--seed 가 필요하다")
    service = Service.from_config()
    created = seed(service.db_path)
    print(f"{service.db_path} — {'생성' if created else '이미 자료가 있어 그대로 둠'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
