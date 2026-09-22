"""가상 MES 운영 데이터 원본 검증 (WBS 4.7.15 · FR-12 · ADR-34).

**이 파일이 지키는 것은 «읽기만 한다» 와 «합성 자료뿐이다» 다.** 값을 잘 읽는지는
고치기 쉬운 문제지만, 이 두 가지가 무너지면 로봇이 **운영 DB 를 고치거나** 실제
개인정보를 읽어 말하게 된다. 둘 다 사람이 알아차리기 전에 일어난다.

그래서 DoD 다섯 항목을 그대로 시험으로 옮긴다.

1. **① 정본은 스키마와 seed** — DB 는 생성물이라 커밋되지 않고, 다시 만들면 같다.
2. **② 네 표** — ADR-34 가 정한 목록을 시험이 **따로 적어** 구현과 대조한다.
3. **③ 출처와 갱신 시각** — 네 표 전수로 붙는지 본다.
4. **④ 읽기 전용** — 함수를 안 만든 것이 아니라 **써지지 않는지** 확인한다.
5. **⑤ 개인정보 0건** — 담을 칸 자체가 없는지, 자료에 연락처 모양이 없는지 본다.

⚠️ **`assist` 모드가 아직 열리지 않아야 한다.** `4.7.16`·`4.7.17` 이 없는 채로 열리면
조회 라우터도 신선도 검사도 없이 현장지원 순찰이 돈다 (FR-11.7).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from host.behavior.mission import available_modes, missing_requirements
from host.factory_ops import store as store_mod
from host.factory_ops.seed import KST, SEED, build
from host.factory_ops.store import (
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE,
    TABLES,
    FactoryOps,
    StoreError,
)

#: ADR-34 «데이터 범위» 가 정한 네 표. **구현에서 가져오지 않고 손으로 적는다** —
#: 같은 곳에서 읽으면 둘이 함께 틀려도 시험은 통과한다.
EXPECTED_TABLES = ("production_status", "shipment_schedule", "work_schedule", "equipment_status")

#: 있어서는 안 되는 칸 이름 조각 (FR-12.5). 값을 검사하는 것보다 **담을 자리가 없는
#: 것**이 확실하다.
FORBIDDEN_COLUMNS = (
    "employee",
    "worker",
    "staff",
    "person",
    "name",
    "phone",
    "tel",
    "contact",
    "email",
    "mail",
    "resident",
    "birth",
    "passphrase",
    "password",
    "secret",
    "token",
)

#: 개인정보 모양의 문자열. 전화번호는 `0` 으로 시작하므로 ISO 날짜와 겹치지 않는다.
CONTACT_SHAPES = (
    re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"),  # 전화번호
    re.compile(r"\b\d{6}-\d{7}\b"),  # 주민등록번호
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # 전자우편
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return build(tmp_path / "factory_ops.db")


@pytest.fixture
def store(db: Path) -> FactoryOps:
    return FactoryOps(db_path=db)


def _columns(db: Path, table: str) -> tuple[str, ...]:
    conn = sqlite3.connect(db)
    try:
        return tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{table}")'))
    finally:
        conn.close()


# ── ② 네 표 (ADR-34 데이터 범위) ──────────────────────────
def test_schema_declares_exactly_the_four_tables(db: Path) -> None:
    """**다섯 번째 표가 조용히 늘지 않는다.**

    실험 구현(`experiments/wonderecho-audio/factory_mes.py`)은 검사 로그까지 다섯
    표를 두었고, 그래서 `DB_GUIDE.md` 가 *"`factory_ops/` 설계와 같다고 보지 않는다"*
    라고 적어 두었다. 범위를 늘리려면 ADR-34 를 먼저 고친다.
    """
    conn = sqlite3.connect(db)
    try:
        found = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert found == set(EXPECTED_TABLES)
    assert set(TABLES) == set(EXPECTED_TABLES)


def test_filterable_columns_exist_in_the_schema(db: Path) -> None:
    """화이트리스트가 스키마와 어긋나면 **조회가 통째로 실패한다.**"""
    for table, columns in TABLES.items():
        actual = _columns(db, table)
        assert set(columns) <= set(actual), f"{table}: {sorted(set(columns) - set(actual))}"
        assert columns[0] == actual[0], f"{table}: 첫 칸은 기본키여야 정렬이 고정된다"


# ── ③ 출처와 갱신 시각 (FR-12.3) ──────────────────────────
def test_every_table_carries_updated_at(db: Path) -> None:
    """신선도 판정(`4.7.17`)이 이 칸 하나에 걸린다."""
    for table in EXPECTED_TABLES:
        assert "updated_at" in _columns(db, table), table


@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_every_reading_carries_source_and_updated_at(store: FactoryOps, table: str) -> None:
    """네 표 전수 — 값만 돌려주는 경로가 하나도 없어야 한다."""
    reading = store.read(table)
    assert reading.rows, f"{table}: 합성 자료가 비었다"
    assert reading.source == SOURCE
    assert datetime.fromisoformat(reading.updated_at)
    for row in reading.rows:
        assert row["updated_at"]


def test_reading_reports_the_oldest_row(db: Path) -> None:
    """**가장 최신이 아니라 가장 오래된 값이다.**

    세 줄을 묶어 답하면서 한 줄이 낡았다면 그 답 전체가 낡은 것이다. 최신값을
    돌려주면 낡은 줄이 신선한 답에 섞여 나간다.
    """
    stale = (datetime.now(KST) - timedelta(days=3)).isoformat(timespec="seconds")
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE production_status SET updated_at = ? WHERE line_id = 'B'", (stale,))
        conn.commit()
    finally:
        conn.close()
    assert FactoryOps(db_path=db).read("production_status").updated_at == stale


# ── ④ 읽기 전용 (DoD ④ · ADR-34 운용규칙) ──────────────────
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO production_status VALUES ('Z','x',1,1,'running','2026-01-01T00:00:00+09:00')",
        "UPDATE production_status SET completed_quantity = 0",
        "DELETE FROM production_status",
        "DROP TABLE production_status",
    ],
)
def test_the_application_path_cannot_write(db: Path, sql: str) -> None:
    """**«쓰는 함수를 안 만들었다» 로는 모자란다.**

    그 약속은 다음 사람이 하나 추가하면 끝난다. 연결이 읽기 전용이면 추가해도
    동작하지 않으므로, 지켜야 할 규칙을 코드가 대신 지킨다.
    """
    conn = store_mod._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(sql)
    finally:
        conn.close()


def test_reading_never_creates_a_missing_database(tmp_path: Path) -> None:
    """없는 DB 를 만들면 **빈 표를 최신 운영값으로 읽는 길**이 생긴다."""
    missing = tmp_path / "없다.db"
    with pytest.raises(StoreError, match="factory_ops_seed"):
        FactoryOps(db_path=missing).read("production_status")
    assert not missing.exists()


def test_unknown_table_is_refused(store: FactoryOps) -> None:
    with pytest.raises(StoreError, match="허용되지 않은 표"):
        store.read("sqlite_master")


def test_unknown_filter_column_is_refused(store: FactoryOps) -> None:
    with pytest.raises(StoreError, match="허용되지 않은 조회 칸"):
        store.read("production_status", updated_at="2026-01-01")


def test_filter_values_are_bound_not_interpolated(store: FactoryOps) -> None:
    """질문이 SQL 이 되는 길이 없어야 한다 (FR-12.2)."""
    assert store.read("production_status", line_id="' OR 1=1 --").rows == ()


def test_a_stale_generated_database_is_refused(db: Path) -> None:
    """DB 는 생성물이라 **낡은 것이 남는다.** 판이 다르면 읽지 않고 멈춘다."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    finally:
        conn.close()
    with pytest.raises(StoreError, match="스키마 판이 다르다"):
        FactoryOps(db_path=db).read("production_status")


# ── ① 정본은 스키마와 seed (DoD ①) ────────────────────────
def test_schema_version_matches_the_code() -> None:
    """`schema.sql` 과 `store.SCHEMA_VERSION` 이 어긋나면 **직접 만든 DB 도 거부된다.**"""
    declared = re.search(r"PRAGMA user_version\s*=\s*(\d+)", SCHEMA.read_text(encoding="utf-8"))
    assert declared and int(declared.group(1)) == SCHEMA_VERSION


def test_build_refuses_to_overwrite_without_force(db: Path) -> None:
    """다시 만들어도 잃을 것은 없지만 **지우는 일은 시키는 사람이 말한다.**"""
    with pytest.raises(StoreError, match="이미 있다"):
        build(db)
    assert build(db, force=True) == db


def test_build_is_deterministic_for_a_given_time(tmp_path: Path) -> None:
    """같은 시각이면 같은 DB — `4.7.17` 의 골든 시험이 이 위에 선다."""
    when = datetime(2026, 9, 22, 9, 0, tzinfo=KST)
    first = FactoryOps(db_path=build(tmp_path / "a.db", now=when))
    second = FactoryOps(db_path=build(tmp_path / "b.db", now=when))
    for table in EXPECTED_TABLES:
        assert first.read(table) == second.read(table), table


def test_relative_dates_follow_the_build_day(tmp_path: Path) -> None:
    """절대 날짜를 적어 두면 **어제 납품했어야 할 일정**이 시연에 남는다."""
    when = datetime(2026, 9, 22, 9, 0, tzinfo=KST)
    reading = FactoryOps(db_path=build(tmp_path / "c.db", now=when)).read("shipment_schedule")
    deadlines = [row["deadline"] for row in reading.rows]
    assert deadlines == ["2026-09-23", "2026-09-25", "2026-09-27"]


# ── ⑤ 합성 자료뿐 (FR-12.5) ───────────────────────────────
@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_no_column_could_hold_personal_data(db: Path, table: str) -> None:
    """**담을 칸이 없으면 실수로 들어갈 수도 없다.**"""
    for column in _columns(db, table):
        bad = [w for w in FORBIDDEN_COLUMNS if w in column.lower()]
        assert not bad, f"{table}.{column}: {bad}"


def test_the_seed_holds_no_contact_shaped_text(db: Path) -> None:
    """전화번호·주민등록번호·전자우편 모양이 한 건도 없어야 한다."""
    texts = [SEED.read_text(encoding="utf-8")]
    conn = sqlite3.connect(db)
    try:
        for table in EXPECTED_TABLES:
            texts += [str(v) for row in conn.execute(f'SELECT * FROM "{table}"') for v in row]
    finally:
        conn.close()
    for shape in CONTACT_SHAPES:
        hits = [t for t in texts if shape.search(t)]
        assert not hits, f"{shape.pattern}: {hits[:3]}"


# ── FR-11.7 — 네 모듈이 다 있어야 열린다 ────────────────────
def test_assist_mode_opens_only_with_router_and_service() -> None:
    """`4.7.16`·`4.7.17` 이 들어와 현장지원 모드가 열린다.

    ⚠️ **모듈 이름을 손으로 적는다.** `REQUIRES` 에서 읽어 오면 그 표를 비웠을 때
    시험도 같이 비어 통과한다. 모드를 여는 조건은 시험이 따로 알고 있어야 한다.
    """
    assert missing_requirements("assist") == ()
    assert "assist" in available_modes()

    import host.behavior.mission as mission_mod

    assert mission_mod.REQUIRES["assist"] == (
        "host.factory_ops.service",
        "host.factory_ops.router",
    )


def test_assist_closes_again_when_a_module_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    """**모듈이 사라지면 모드도 닫힌다.**

    capability 검사가 설정 플래그가 아니라 모듈 존재를 묻는 이유가 이것이다
    (FR-11.7). 기능을 지우고 표기만 남기는 길이 없어야 한다.
    """
    import host.behavior.mission as mission_mod

    monkeypatch.setitem(mission_mod.REQUIRES, "assist", ("host.factory_ops.nope",))
    assert missing_requirements("assist") == ("host.factory_ops.nope",)
    assert "assist" not in available_modes()
