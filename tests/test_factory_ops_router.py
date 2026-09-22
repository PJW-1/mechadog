"""현장지원 질의 라우터·신선도 계약 (WBS 4.7.16 · 4.7.17 · FR-12.2~12.6 · ADR-34).

⚠️ **답변 문장을 통째로 고정한다** (`4.7.17` DoD ④). 숫자·날짜·상태를 부분 문자열로
만 보면, 템플릿이 «약 400개» 로 바뀌어도 시험이 통과한다. 이 시험이 막으려는 것이
바로 그 «자연스럽게 다듬기» 이므로 기대값을 한 글자도 빼지 않고 적는다.

⚠️ **규칙표 순서를 시험이 고정한다.** 「A라인 설비상태 어때」가 생산 조회로 잡히는
것은 규칙 한 줄을 옮기면 언제든 다시 생긴다. 사람이 읽어서 알아채기 어려운 종류의
회귀라 시험으로 못 박는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from host.factory_ops import router as router_mod
from host.factory_ops.router import (
    CHECK_PROCEDURE,
    NO_HISTORY,
    UNAVAILABLE,
    answer,
    route,
)
from host.factory_ops.seed import KST, build
from host.factory_ops.service import (
    DEFAULT_TTL_S,
    ENDPOINTS,
    Service,
    SourceError,
    UnknownLineError,
    mark_freshness,
)
from host.factory_ops.store import SOURCE, FactoryOps

#: 자료를 만든 시각. 답변 꼬리의 «9시 0분» 이 여기서 나온다.
BUILD_AT = datetime(2026, 9, 22, 9, 0, tzinfo=KST)

#: 발화에 실리는 출처 표기. 기록에 남는 이름(`demo-mes`)과 다르다.
SPOKEN_SOURCE = "데모 MES"

TAIL = f"{SPOKEN_SOURCE}의 9시 0분 자료입니다."


@pytest.fixture
def service(tmp_path: Path) -> Service:
    build(tmp_path / "ops.db", now=BUILD_AT)
    return Service(FactoryOps(db_path=tmp_path / "ops.db"))


@pytest.fixture
def providers(service: Service) -> dict[str, Any]:
    return {"mes": _At(service, BUILD_AT)}


class _At:
    """`answer()` 가 시각을 넘기지 않으므로 시험에서 고정해 준다."""

    def __init__(self, service: Service, at: datetime) -> None:
        self._service = service
        self._at = at

    def query(self, endpoint: str, params: Any = None) -> list[dict[str, Any]]:
        return self._service.query(endpoint, params, at=self._at)


# ── 4.7.16 규칙표 분기 ────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "endpoint"),
    [
        ("A라인 생산량 얼마나 됐어", "production"),
        ("라인 현황 알려줘", "production"),
        ("어느 라인이 가동 중이야", "production"),
        ("출하 일정 알려줘", "shipments"),
        ("납기 언제야", "shipments"),
        ("작업지시 뭐 있어", "schedule"),
        ("우선순위 높은 작업 뭐야", "schedule"),
        ("설비 상태 어때", "equipment"),
        ("고장난 설비 있어", "equipment"),
    ],
)
def test_route_picks_the_expected_group(text: str, endpoint: str) -> None:
    hit = route(text)
    assert hit is not None
    assert (hit.group, hit.endpoint) == ("mes", endpoint)


def test_equipment_rules_win_over_the_broad_state_keyword() -> None:
    """⚠️ **회귀 고정 — 「A라인 설비상태」가 생산으로 가면 안 된다.**

    넓은 말인 `상태` 가 설비 규칙보다 앞에 있으면, 라인 표기가 있어서
    `needs_line` 까지 통과해 **설비를 물었는데 생산 실적이 나온다.** 규칙표에서
    설비 줄을 아래로 옮기는 순간 되살아나는 종류의 결함이다.
    """
    hit = route("A라인 설비상태 어때")
    assert hit is not None
    assert hit.endpoint == "equipment"
    assert hit.params == {"line": "A"}


def test_broad_keyword_needs_a_line_marker() -> None:
    """라인 표기가 없는 「지금 상태 어때」는 운영정보 질의가 아니다."""
    assert route("지금 상태 어때") is None
    hit = route("B라인 지금 돌아가?")
    assert hit is not None
    assert (hit.endpoint, hit.params) == ("production", {"line": "B"})


def test_line_marker_filters_when_present_and_not_otherwise() -> None:
    """「라인 현황」은 전체를, 「A라인 현황」은 그 라인만 묻는 말이다."""
    assert route("라인 현황 알려줘").params == {}
    assert route("A라인 현황 알려줘").params == {"line": "A"}


def test_unrelated_question_is_not_ours() -> None:
    """**모르면 `None` 이다.** 안전 문서나 LLM 경로로 넘어갈 질문이다."""
    assert route("소화기 어디 있어") is None
    assert route("") is None


def test_lowercase_line_marker_is_normalised() -> None:
    assert route("a라인 생산량").params == {"line": "A"}


# ── 4.7.17 신선도 판정 ────────────────────────────────────────
def test_fresh_and_stale_rows_are_marked(service: Service) -> None:
    rows = service.query("production", at=BUILD_AT)
    assert [row["fresh"] for row in rows] == [True, True, True]
    assert {row["source"] for row in rows} == {SOURCE}

    late = BUILD_AT + timedelta(seconds=DEFAULT_TTL_S + 1)
    assert [row["fresh"] for row in service.query("production", at=late)] == [False] * 3


def test_future_timestamps_are_not_fresh() -> None:
    """⚠️ **원본 시계가 앞서면 TTL 이 아무것도 막지 못한다.**"""
    rows = [{"updated_at": BUILD_AT.isoformat(timespec="seconds")}]
    marked = mark_freshness(rows, DEFAULT_TTL_S, source=SOURCE, at=BUILD_AT - timedelta(hours=1))
    assert marked[0]["fresh"] is False


@pytest.mark.parametrize("value", [None, "", "어제", 12345])
def test_unreadable_timestamp_is_not_fresh(value: Any) -> None:
    marked = mark_freshness([{"updated_at": value}], DEFAULT_TTL_S, source=SOURCE, at=BUILD_AT)
    assert marked[0]["fresh"] is False


def test_ttl_is_per_item(service: Service) -> None:
    """항목별 TTL (`4.7.17` DoD ①). 적히지 않은 항목은 기본값으로 떨어진다."""
    service.ttl_s = {"production": 60, "shipments": "이상한 값"}
    assert service.ttl_for("production") == 60
    assert service.ttl_for("shipments") == DEFAULT_TTL_S
    assert service.ttl_for("equipment") == DEFAULT_TTL_S


def test_config_ttl_covers_every_endpoint() -> None:
    """`config.yaml` 이 네 조회군 모두의 TTL 을 적어 두어야 한다."""
    from host.common.config import load_base_config

    section = load_base_config()["factory_ops"]
    assert set(section["ttl_s"]) == set(ENDPOINTS)
    assert all(float(v) > 0 for v in section["ttl_s"].values())


# ── 조회 계층 ────────────────────────────────────────────────
def test_production_carries_the_remaining_quantity(service: Service) -> None:
    rows = service.query("production", at=BUILD_AT)
    assert [row["remaining_quantity"] for row in rows] == [420, 0, 390]


def test_schedule_drops_done_and_sorts_by_priority(service: Service) -> None:
    rows = service.query("schedule", at=BUILD_AT)
    assert [row["task_id"] for row in rows] == ["WO-1001", "WO-1002", "WO-1003"]
    assert all(row["status"] != "done" for row in rows)


def test_shipment_window_is_a_bound_not_a_suggestion(service: Service) -> None:
    """납기 범위는 파이썬에서 거른다. SQL 표면을 넓히지 않기 위해서다."""
    assert len(service.query("shipments", at=BUILD_AT)) == 3
    assert len(service.query("shipments", {"days": 2}, at=BUILD_AT)) == 1
    assert service.query("shipments", {"days": 0}, at=BUILD_AT) == []


@pytest.mark.parametrize("days", [None, "사흘", -5])
def test_bad_day_counts_fall_back_or_clamp(service: Service, days: Any) -> None:
    rows = service.query("shipments", {"days": days}, at=BUILD_AT)
    assert isinstance(rows, list)


def test_known_lines_are_read_from_the_data(service: Service) -> None:
    assert service.known_lines() == ("A", "B", "C")


def test_unknown_line_is_a_failure_not_an_empty_answer(service: Service) -> None:
    """⚠️ **「D라인 작업 없습니다」 는 D라인이 한가하다는 뜻으로 들린다.**"""
    with pytest.raises(UnknownLineError) as caught:
        service.query("schedule", {"line": "D"}, at=BUILD_AT)
    assert caught.value.valid == ("A", "B", "C")


def test_unknown_endpoint_is_refused(service: Service) -> None:
    with pytest.raises(SourceError, match="모르는 조회군"):
        service.query("payroll", at=BUILD_AT)


def test_missing_database_is_reported_not_created(tmp_path: Path) -> None:
    """**없는 DB 를 만들지 않는다.** 빈 표를 최신값으로 읽는 길을 두지 않는다."""
    service = Service(FactoryOps(db_path=tmp_path / "없다.db"))
    with pytest.raises(SourceError, match="읽을 수 없다"):
        service.query("production", at=BUILD_AT)
    assert not (tmp_path / "없다.db").exists()


def test_query_text_never_reaches_sql(service: Service) -> None:
    """라인 이름은 등록 목록과 대조한 뒤에야 바인딩 파라미터가 된다 (FR-12.2)."""
    with pytest.raises(UnknownLineError):
        service.query("production", {"line": "A'; DROP TABLE production_status; --"}, at=BUILD_AT)
    assert len(service.query("production", at=BUILD_AT)) == 3


def test_service_reads_its_settings_from_config() -> None:
    from host.common.config import load_base_config

    service = Service.from_config(load_base_config())
    assert service.ops.db_path.name == "factory_ops.db"
    assert service.ttl_for("production") == 3600


@pytest.mark.parametrize(
    "section",
    [None, "문자열", {}, {"db_path": ""}, {"db_path": 5}],
)
def test_broken_config_section_is_refused(section: Any) -> None:
    with pytest.raises(SourceError):
        Service.from_config({"factory_ops": section} if section is not None else {})


# ── 답변 조립: 문장을 통째로 고정한다 (4.7.17 DoD ④) ──────────
def test_production_answer_is_deterministic(providers: dict[str, Any]) -> None:
    assert answer("라인 현황 알려줘", providers) == (
        "A라인은 목표 1,200개 중 780개를 완료해 420개 남았고 현재 가동 중입니다."
        " B라인은 목표 800개 중 800개를 완료해 0개 남았고 현재 대기입니다."
        " C라인은 목표 600개 중 210개를 완료해 390개 남았고 현재 정지입니다."
        f" {TAIL}"
    )


def test_shipment_answer_is_deterministic(providers: dict[str, Any]) -> None:
    """⚠️ 조사 «으로/로» 까지 고정한다. 「가나정밀으로」 가 나오면 TTS 가 읽는다."""
    assert answer("출하 일정 알려줘", providers) == (
        "예정된 출하가 3건 있습니다."
        " 9월 23일에 가나정밀로 MD-100 구동모듈 400개,"
        " 9월 25일에 나라기계로 MD-200 센서모듈 300개,"
        " 9월 27일에 다온물산으로 MD-100 구동모듈 600개."
        f" {TAIL}"
    )


def test_schedule_answer_is_deterministic(providers: dict[str, Any]) -> None:
    assert answer("작업지시 뭐 있어", providers) == (
        "가장 급한 작업은 A라인 'MD-100 구동모듈 잔량 생산'이고 진행 중입니다."
        " 나머지 작업지시가 2건 있습니다."
        f" {TAIL}"
    )


def test_equipment_answer_reports_state_not_a_check_log(providers: dict[str, Any]) -> None:
    """⚠️ **「지금 돌아가나요」에 답할 수 있어야 한다** (`4.7.15` DoD ②).

    점검 이력 표로는 점검 항목과 결과밖에 말할 수 없다. 여기서 확인하는 것은
    가동 상태(`running`·`maintenance`·`fault`)가 그대로 발화된다는 사실이다.
    """
    assert answer("설비 상태 어때", providers) == (
        "주의가 필요한 설비가 2대 있습니다."
        " 무인운반차-02(B라인) 정비 중, 컨베이어-03(C라인) 고장."
        f" {CHECK_PROCEDURE} {TAIL}"
    )


def test_fault_question_never_diagnoses(providers: dict[str, Any]) -> None:
    """고장 질문에는 원인이 아니라 점검 절차를 붙인다 (`4.7.17` DoD ⑤)."""
    said = answer("고장난 설비 있어", providers)
    assert said is not None
    assert CHECK_PROCEDURE in said
    assert "원인" not in said.replace(CHECK_PROCEDURE, "")


def test_all_running_lines_report_normal(providers: dict[str, Any]) -> None:
    said = answer("A라인 설비상태 어때", providers)
    assert said == f"설비 1대가 모두 정상입니다. {TAIL}"


def test_history_question_says_what_we_do_not_have(providers: dict[str, Any]) -> None:
    """⚠️ **이력을 물으면 없다고 먼저 말한다.** 현재 상태를 이력으로 듣게 두지 않는다."""
    said = answer("설비 점검기록 보여줘", providers)
    assert said is not None
    assert said.startswith(NO_HISTORY)


def test_unknown_line_answer_lists_the_registered_ones(providers: dict[str, Any]) -> None:
    assert answer("D라인 생산량 얼마야", providers) == (
        "그 라인은 등록되어 있지 않습니다. 등록된 라인은 A, B, C입니다."
    )


def test_unrelated_question_returns_none(providers: dict[str, Any]) -> None:
    assert answer("소화기 어디 있어", providers) is None


def test_missing_provider_refuses_instead_of_guessing() -> None:
    assert answer("출하 일정 알려줘", {}) == UNAVAILABLE


def test_source_failure_refuses_instead_of_guessing(tmp_path: Path) -> None:
    """⚠️ **원본이 죽은 날 질문이 LLM 으로 흘러가면 그럴듯한 숫자가 나온다.**"""
    broken = Service(FactoryOps(db_path=tmp_path / "없다.db"))
    assert answer("출하 일정 알려줘", {"mes": broken}) == UNAVAILABLE


def test_stale_data_is_refused_not_estimated(service: Service) -> None:
    """TTL 을 넘기면 마지막 값을 말하지 않고 닫는다 (`4.7.17` DoD ③)."""
    late = BUILD_AT + timedelta(seconds=DEFAULT_TTL_S + 1)
    said = answer("라인 현황 알려줘", {"mes": _At(service, late)})
    assert said == f"생산 정보의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"


def test_partially_stale_answer_says_how_many_were_dropped() -> None:
    """일부만 낡았으면 답하되 **몇 건을 뺐는지 밝힌다** (`4.7.17` DoD ②)."""
    rows = [
        {
            "line_id": "A",
            "target_quantity": 10,
            "completed_quantity": 4,
            "remaining_quantity": 6,
            "state": "running",
            "source": SOURCE,
            "updated_at": BUILD_AT.isoformat(timespec="seconds"),
            "fresh": True,
        },
        {"line_id": "B", "fresh": False},
    ]
    said = router_mod._fmt_production(rows)
    assert "오래된 자료 1건은 답변에서 제외했습니다." in said


def test_empty_result_is_not_an_error_message() -> None:
    assert router_mod._fmt_production([]) == "등록된 생산 라인이 없습니다."
    assert router_mod._fmt_shipments([]) == "앞으로 7일 이내 출하 예정이 없습니다."
    assert router_mod._fmt_schedule([]) == "진행 중이거나 대기 중인 작업지시가 없습니다."
    assert router_mod._fmt_equipment([]) == "등록된 설비가 없습니다."


@pytest.mark.parametrize(
    ("word", "particle"),
    [("가나정밀", "로"), ("나라기계", "로"), ("다온물산", "으로"), ("", "로")],
)
def test_particle_follows_the_final_consonant(word: str, particle: str) -> None:
    assert router_mod._euro(word) == particle


def test_date_is_spoken_not_printed() -> None:
    assert router_mod._ko_date("2026-09-18") == "9월 18일"
    assert router_mod._ko_date("미정") == "미정"


def test_source_tail_survives_a_broken_timestamp() -> None:
    assert router_mod._source_tail({"updated_at": "망가짐"}, 0) == ""
    assert router_mod._source_tail({}, 2) == "오래된 자료 2건은 답변에서 제외했습니다."


def test_shipment_answer_summarises_beyond_four_rows() -> None:
    rows = [
        {
            "deadline": f"2026-09-2{n}",
            "customer": "가나정밀",
            "product": "MD-100 구동모듈",
            "quantity": 10,
            "source": SOURCE,
            "updated_at": BUILD_AT.isoformat(timespec="seconds"),
            "fresh": True,
        }
        for n in range(1, 6)
    ]
    said = router_mod._fmt_shipments(rows)
    assert "예정된 출하가 5건 있습니다." in said
    assert "외 1건" in said
