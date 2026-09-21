"""현장지원 운영정보 경로 — 원본·라우터·신선도 계약 (WBS 4.7.15~4.7.17 · ADR-34).

**골든 시험의 목적은 LLM 이 숫자를 못 바꾸게 하는 것이다** (`4.7.17` DoD ④).
답변 문장은 코드가 찍으므로, 조회값이 문장에 그대로 나타나는지를 본다.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from host.common.config import ConfigError
from host.factory_ops import router, service
from host.factory_ops.service import Service, SourceError, UnknownLineError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "factory_ops.db"
    assert service.seed(path) is True
    return path


@pytest.fixture
def svc(db: Path) -> Service:
    return Service(db, {"production": 3600, "shipments": 86400, "schedule": 21600})


# ── 원본 (4.7.15) ───────────────────────────────────────────


def test_schema_has_exactly_the_four_tables_adr34_scoped(db: Path) -> None:
    """ADR-34 「데이터 범위」는 **네 개**다. 늘리려면 ADR 을 고친다."""
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert names == set(service.TABLES)


def test_seed_never_overwrites_existing_rows(db: Path) -> None:
    """두 번째 `--seed` 는 **아무것도 하지 않는다** — 운용 DB 를 날리는 길 없음."""
    assert service.seed(db) is False
    assert len(Service(db).query("production")) == 3


def test_the_operational_path_cannot_write(svc: Service) -> None:
    """`4.7.15` DoD ④ — 읽기 전용을 주석이 아니라 **연결 플래그로** 지킨다."""
    conn = service._open_readonly(svc.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM production_status")
    finally:
        conn.close()


def test_a_missing_database_says_how_to_make_one(tmp_path: Path) -> None:
    """빈 DB 로 «자료 없음» 을 답하면 고장과 «일정 없음» 이 같은 문장이 된다."""
    with pytest.raises(SourceError, match="--seed"):
        Service(tmp_path / "nope.db").query("production")


def test_source_error_is_a_config_error() -> None:
    """`runtime` 의 설정 오류 경로가 새 except 절 없이 이것도 잡아야 한다."""
    assert issubclass(SourceError, ConfigError)


def test_seed_holds_no_real_personal_data() -> None:
    """`4.7.15` DoD ⑤ — 합성 자료다. 사람 이름 칸 자체가 스키마에 없다."""
    schema = service.SCHEMA_PATH.read_text(encoding="utf-8")
    for forbidden in ("employee", "worker", "person", "passphrase", "password"):
        assert forbidden not in schema.lower()


# ── 신선도 계약 (4.7.17) ────────────────────────────────────


def test_ttl_is_per_endpoint_from_config(svc: Service) -> None:
    """`4.7.17` DoD ① — 항목마다 다르고, 안 적힌 항목은 기본값."""
    assert svc.ttl_for("production") == 3600
    assert svc.ttl_for("shipments") == 86400
    assert svc.ttl_for("equipment") == service.DEFAULT_TTL_S


def test_config_section_carries_every_endpoint_ttl() -> None:
    """설정이 정본이다 — 네 조회군의 TTL 이 `config.yaml` 에 다 있어야 한다."""
    configured = Service.from_config()
    assert set(configured.ttl_s) == set(service.ENDPOINTS)


def test_rows_carry_source_and_freshness(svc: Service) -> None:
    """`4.7.17` DoD ② — 행마다 `source`·`updated_at`·`fresh`."""
    for row in svc.query("production"):
        assert row["source"] == service.SOURCE
        assert row["fresh"] is True
        assert row["updated_at"]


def test_values_older_than_the_ttl_are_refused_not_guessed(svc: Service) -> None:
    """`4.7.17` DoD ③ — TTL 초과는 **값을 내지 않고** 확인 불가로 답한다."""
    later = service.now() + timedelta(seconds=3601 + 60)
    rows = svc.query("production", at=later)
    assert [r["fresh"] for r in rows] == [False, False, False]
    spoken = router._fmt_production(rows)
    assert router.UNAVAILABLE in spoken
    assert "1,200" not in spoken, "오래된 값이 문장에 새면 TTL 을 둔 뜻이 없다"


def test_a_future_timestamp_is_not_fresh() -> None:
    """시계가 어긋난 원본을 «방금 자료» 로 읽으면 TTL 이 아무것도 막지 못한다."""
    rows = service.mark_freshness(
        [{"updated_at": service.now().isoformat()}], 3600, at=service.now() - timedelta(hours=2)
    )
    assert rows[0]["fresh"] is False


def test_broken_timestamps_are_stale_rather_than_crashing() -> None:
    rows = service.mark_freshness([{"updated_at": "어제"}, {}], 3600)
    assert [r["fresh"] for r in rows] == [False, False]


# ── 라우터 (4.7.16) ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "endpoint"),
    [
        ("A라인 생산량 얼마나 됐어요", "production"),
        ("라인 상태 어때요", "production"),
        ("이번 주 출하 일정 알려줘", "shipments"),
        ("납기 언제예요", "shipments"),
        ("작업지시 뭐부터 해요", "schedule"),
        ("설비 점검 기록 보여줘", "equipment"),
        ("C라인 고장났어요?", "equipment"),
    ],
)
def test_routing_is_deterministic(text: str, endpoint: str) -> None:
    hit = router.route(text)
    assert hit is not None and hit.endpoint == endpoint and hit.group == "mes"


@pytest.mark.parametrize("text", ["오늘 날씨 어때", "화장실 어디예요", "안녕하세요"])
def test_unrelated_questions_are_not_claimed(text: str) -> None:
    """모르는 질문은 `None` — 이 경로가 삼키면 안전 문서·LLM 이 못 받는다."""
    assert router.route(text) is None
    assert router.answer(text, {}) is None


def test_bare_state_words_need_a_line_marker() -> None:
    """「지금 상태 어때」를 생산 조회로 잡지 않는다 (`needs_line`)."""
    assert router.route("지금 상태 어때") is None
    assert router.route("B라인 상태 어때").endpoint == "production"


def test_specific_keywords_win_over_broad_ones() -> None:
    """「라인상태」가 「상태」보다 규칙표에서 앞에 있어야 한다."""
    assert router.route("라인상태 알려줘").params == {}


def test_the_router_never_builds_sql_from_text(svc: Service) -> None:
    """`4.7.16` — 파라미터는 라인 ID 와 일수뿐이다."""
    hit = router.route("A라인'; DROP TABLE production_status; -- 생산량")
    assert hit is not None and hit.params == {"line": "A"}
    assert len(svc.query(hit.endpoint, hit.params)) == 1


def test_an_unknown_line_is_a_failure_not_an_empty_answer(svc: Service) -> None:
    """«없는 라인» 과 «기록 0건» 을 구분한다 — 안 그러면 한가한 줄 안다."""
    with pytest.raises(UnknownLineError):
        svc.query("production", {"line": "D"})
    spoken = router.answer("D라인 생산량", {"mes": svc})
    assert "등록되어 있지 않습니다" in spoken and "A, B, C" in spoken


def test_a_dead_source_speaks_instead_of_falling_through(tmp_path: Path) -> None:
    """원본이 죽은 날 질문이 LLM 으로 새면 **그럴듯한 숫자**가 나온다."""
    dead = Service(tmp_path / "gone.db")
    assert router.answer("생산량 알려줘", {"mes": dead}) == router.UNAVAILABLE
    assert router.answer("생산량 알려줘", {}) == router.UNAVAILABLE


# ── 답변 (골든 · 4.7.17 DoD ④) ──────────────────────────────


def test_production_answer_reports_the_stored_numbers(svc: Service) -> None:
    rows = svc.query("production", {"line": "A"})
    spoken = router.answer("A라인 생산량", {"mes": svc})
    assert f"{rows[0]['target_quantity']:,}개" in spoken
    assert f"{rows[0]['completed_quantity']:,}개" in spoken
    assert f"{rows[0]['remaining_quantity']:,}개" in spoken
    assert "가동 중" in spoken
    assert service.SOURCE in spoken


def test_shipment_answer_reads_the_stored_deadline(svc: Service) -> None:
    rows = svc.query("shipments")
    spoken = router.answer("출하 일정", {"mes": svc})
    assert f"{len(rows)}건" in spoken
    assert router._ko_date(rows[0]["deadline"]) in spoken


@pytest.mark.parametrize(
    ("word", "particle"),
    [("한국정밀", "로"), ("대성산업", "으로"), ("도크", "로"), ("공장", "으로")],
)
def test_the_particle_follows_the_final_consonant(word: str, particle: str) -> None:
    """ㄹ 받침은 «로» 다 — 받침 유무만 보면 「한국정밀으로」가 TTS 로 나간다."""
    assert router._euro(word) == particle


def test_schedule_answer_leads_with_the_top_priority(svc: Service) -> None:
    rows = svc.query("schedule")
    spoken = router.answer("작업지시 알려줘", {"mes": svc})
    assert rows[0]["description"] in spoken
    assert f"{len(rows) - 1}건" in spoken


def test_a_fault_question_points_at_the_procedure_instead_of_diagnosing(svc: Service) -> None:
    """ADR-34 운용규칙 6 — 진단하지 않는다."""
    spoken = router.answer("설비 상태 어때요", {"mes": svc})
    assert "주의" in spoken
    assert router.CHECK_PROCEDURE in spoken


def test_a_healthy_fleet_does_not_get_the_procedure_tail(svc: Service) -> None:
    spoken = router.answer("B라인 설비 점검 기록", {"mes": svc})
    assert "모든 설비가 정상입니다" in spoken
    assert router.CHECK_PROCEDURE not in spoken


def test_stale_rows_are_dropped_and_counted(svc: Service) -> None:
    rows = svc.query("schedule")
    rows[1]["fresh"] = False
    spoken = router._fmt_schedule(rows)
    assert "오래된 자료 1건은 답변에서 제외했습니다." in spoken


# ── 모드 게이트 (FR-11.7) ───────────────────────────────────


def test_assist_mode_is_now_choosable() -> None:
    """이 패키지가 있다는 사실이 곧 `assist` 의 기동 조건이다."""
    from host.behavior.mission import available_modes, missing_requirements

    assert missing_requirements("assist") == ()
    assert "assist" in available_modes()
