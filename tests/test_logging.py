"""JSON Lines 로거 검증 (WBS 4.4.2 · NFR-3④ · ENGINEERING_GUIDE 1절).

**통신 규약과 같은 방식이다.** 골든 픽스처를 정본으로 두고 코드가 그것을 물린다 —
문서에 *"컨텍스트를 넣으세요"* 라고 적는 것으로는 반드시 빠지기 때문이다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FIXTURES, FakeClock, load_jsonl

from host.common.logging_setup import (
    LEVELS,
    REQUIRED_FIELDS,
    EdgeTrigger,
    JsonlFormatter,
    LogContext,
    PeriodicSummary,
    event_logger,
    record_error,
    setup_logging,
)


def strip_case(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


@pytest.fixture
def logs(tmp_path: Path, cfg: dict) -> Iterator[tuple[Path, LogContext]]:
    """실제 파일에 쓰는 로거. 콘솔은 끈다 — 시험 출력이 지저분해진다."""
    ctx = setup_logging(cfg, device_id="mechdog-01", log_dir=tmp_path, console=False)
    yield tmp_path / "mechdog-01.jsonl", ctx
    for handler in list(logging.getLogger("mechadog").handlers):
        logging.getLogger("mechadog").removeHandler(handler)
        handler.close()


def read(path: Path) -> list[dict]:
    logging.shutdown()
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ── 골든 픽스처 (정본) ───────────────────────────────────────
def test_required_context_is_exactly_six_fields() -> None:
    """**6개다** (ENGINEERING_GUIDE 1.1). 늘거나 줄면 픽스처부터 고쳐야 한다."""
    assert set(REQUIRED_FIELDS) == {"ts", "level", "device_id", "seq", "state", "escalation"}


def test_every_valid_fixture_passes() -> None:
    for row in load_jsonl(FIXTURES / "log_samples.jsonl"):
        error = record_error(strip_case(row))
        assert not error, f"{row.get('_case', '')}: {error}"


def test_every_invalid_fixture_is_caught_with_the_stated_reason() -> None:
    """**사유까지 대조한다.** 다른 이유로 걸리면 시험이 우연히 통과한 것이다."""
    for row in load_jsonl(FIXTURES / "log_invalid.jsonl"):
        error = record_error(strip_case(row))
        case = row.get("_case", "")
        assert error, f"{case}: 걸러져야 한다"
        assert row["_expect"] in error, f"{case}: 사유가 {error!r} 로 나왔다"


def test_fixtures_cover_all_four_levels() -> None:
    """레벨 4종이 픽스처에 다 있어야 한다 — DoD 조항이다."""
    levels = {strip_case(r)["level"] for r in load_jsonl(FIXTURES / "log_samples.jsonl")}
    assert levels == LEVELS, f"빠진 레벨: {LEVELS - levels}"


def test_fixtures_cover_edge_trigger_and_periodic_summary() -> None:
    """엣지 트리거와 주기 요약 예가 함께 있어야 한다 — 샘플링이 설계의 일부다."""
    events = {strip_case(r)["event"] for r in load_jsonl(FIXTURES / "log_samples.jsonl")}
    assert "fsm_transition" in events, "엣지 트리거 예"
    assert any(e.endswith("_summary") for e in events), "주기 요약 예"


def test_fixtures_cover_the_situational_context() -> None:
    rows = [strip_case(r) for r in load_jsonl(FIXTURES / "log_samples.jsonl")]
    assert any("track_id" in r for r in rows), "사람 관련 이벤트 (FR-3.6)"
    assert any("zone" in r for r in rows), "구역 관련 이벤트 (FR-8)"


def test_failsafe_records_carry_escalation_f() -> None:
    """로봇이 쓰러져 있는데 `L0`(정상 순찰)이 찍히면 **로그가 거짓을 말한다.**"""
    for row in load_jsonl(FIXTURES / "log_samples.jsonl"):
        clean = strip_case(row)
        if clean["state"] == "FAILSAFE":
            assert clean["escalation"] == "F", row.get("_case", "")


# ── 실제로 찍어 본다 ─────────────────────────────────────────
def test_written_records_pass_their_own_validator(logs) -> None:
    """**우리가 찍은 줄이 우리 검증을 통과해야 한다.**

    픽스처만 검사하면 픽스처와 구현이 각각 진화한다 — 통신 규약에서 겪은 것과
    같은 형태다.
    """
    path, ctx = logs
    log = event_logger("mechadog.test")
    ctx.observe(seq=1234, state="PATROL")
    log.info("fsm_transition", **{"from": "IDLE", "to": "PATROL", "trigger": "START_PATROL"})
    log.warning("telemetry_discarded", reason="seq 역전·중복")
    ctx.observe(state="FAILSAFE")
    log.error("failsafe_entered", trigger="ONBOARD_FAILSAFE")

    rows = read(path)
    assert len(rows) == 3
    for row in rows:
        assert not record_error(row), row


def test_context_is_injected_without_the_caller_remembering(logs) -> None:
    """호출부는 사건 이름과 근거만 적는다 — 컨텍스트는 필터가 넣는다."""
    path, ctx = logs
    ctx.observe(seq=77, state="ALERT")
    event_logger("mechadog.test").info("person_found", conf=0.91)
    row = read(path)[0]
    assert row["device_id"] == "mechdog-01"
    assert row["seq"] == 77
    assert row["state"] == "ALERT"
    assert row["detail"] == {"conf": 0.91}


def test_escalation_follows_the_state(logs) -> None:
    path, ctx = logs
    log = event_logger("mechadog.test")
    ctx.observe(state="PATROL")
    log.info("a")
    ctx.observe(state="FAILSAFE")
    log.info("b")
    ctx.observe(state="IDLE")
    log.info("c")
    assert [r["escalation"] for r in read(path)] == ["L0", "F", "L0"]


def test_level_comes_from_config(cfg: dict, tmp_path: Path) -> None:
    """`DEBUG` 는 매 명령이라 기본 레벨에서 파일에 남지 않아야 한다."""
    assert cfg["logging"]["level"] == "INFO", "정본이 바뀌면 이 시험을 다시 본다"
    ctx = setup_logging(cfg, device_id="mechdog-01", log_dir=tmp_path, console=False)
    log = event_logger("mechadog.test")
    ctx.observe(seq=1, state="PATROL")
    log.debug("cmd_sent", type="MOVE")
    log.info("fsm_transition", **{"from": "IDLE", "to": "PATROL"})
    rows = read(tmp_path / "mechdog-01.jsonl")
    assert [r["event"] for r in rows] == ["fsm_transition"]


def test_python_warning_is_written_as_warn(logs) -> None:
    """정본이 `WARN` 이다 — 파이썬 이름을 그대로 흘리면 픽스처와 어긋난다."""
    path, ctx = logs
    ctx.observe(seq=1, state="PATROL")
    event_logger("mechadog.test").warning("stream_reconnect", backoff_s=2)
    assert read(path)[0]["level"] == "WARN"


def test_ts_is_epoch_milliseconds(logs) -> None:
    """프로토콜의 `ts` 와 같은 축이어야 로그와 패킷을 나란히 놓을 수 있다."""
    from host.common.protocol import system_clock_ms

    path, ctx = logs
    ctx.observe(seq=1, state="PATROL")
    before = system_clock_ms()
    event_logger("mechadog.test").info("probe")
    row = read(path)[0]
    assert isinstance(row["ts"], int)
    assert before - 2000 <= row["ts"] <= system_clock_ms() + 2000


def test_rotation_uses_config_values(cfg: dict, tmp_path: Path) -> None:
    setup_logging(cfg, device_id="mechdog-01", log_dir=tmp_path, console=False)
    handler = next(
        h
        for h in logging.getLogger("mechadog").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert handler.maxBytes == int(cfg["logging"]["rotate_mb"]) * 1024 * 1024
    assert handler.backupCount == int(cfg["logging"]["rotate_keep"])


def test_rotation_actually_rolls_over(cfg: dict, tmp_path: Path) -> None:
    """**설정값만 확인하면 회전이 되는지는 모른다.** 실제로 넘겨 본다."""
    small = dict(cfg)
    small["logging"] = dict(cfg["logging"], rotate_mb=1, rotate_keep=2)
    ctx = setup_logging(small, device_id="mechdog-01", log_dir=tmp_path, console=False)
    handler = next(
        h
        for h in logging.getLogger("mechadog").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    handler.maxBytes = 2000  # 1MB 를 채우려면 시험이 느려진다
    log = event_logger("mechadog.test")
    ctx.observe(seq=1, state="PATROL")
    for i in range(200):
        log.info("filler", i=i, padding="x" * 50)
    logging.shutdown()
    assert (tmp_path / "mechdog-01.jsonl").exists()
    assert (tmp_path / "mechdog-01.jsonl.1").exists(), "회전이 일어나야 한다"
    assert not (tmp_path / "mechdog-01.jsonl.3").exists(), "보관 개수를 넘기지 않는다"


def test_setup_twice_does_not_duplicate_lines(cfg: dict, tmp_path: Path) -> None:
    """두 번 부르면 줄이 두 번 찍힌다 — 대시보드 재기동에서 실제로 겪을 자리다."""
    setup_logging(cfg, device_id="mechdog-01", log_dir=tmp_path, console=False)
    ctx = setup_logging(cfg, device_id="mechdog-01", log_dir=tmp_path, console=False)
    ctx.observe(seq=1, state="PATROL")
    event_logger("mechadog.test").info("once")
    assert len(read(tmp_path / "mechdog-01.jsonl")) == 1


def test_exception_detail_is_captured(logs) -> None:
    path, ctx = logs
    ctx.observe(seq=1, state="PATROL")
    logger = logging.getLogger("mechadog.test")
    try:
        raise ValueError("깨진 응답")
    except ValueError:
        logger.error("vlm_failed", exc_info=True, extra={"event": "vlm_failed"})
    row = read(path)[0]
    assert "ValueError" in row["detail"]["exc"]


# ── 샘플링 (1.3) ─────────────────────────────────────────────
def test_edge_trigger_fires_only_on_change() -> None:
    """`PATROL 유지 중` 을 9천 번 남기지 않기 위한 도구다."""
    edge = EdgeTrigger()
    assert edge.changed("state", "PATROL") is True
    for _ in range(100):
        assert edge.changed("state", "PATROL") is False
    assert edge.changed("state", "SCAN") is True


def test_edge_trigger_separates_keys() -> None:
    edge = EdgeTrigger()
    assert edge.changed("state", "PATROL") is True
    assert edge.changed("link", "PATROL") is True, "키가 다르면 별개다"


def test_edge_trigger_can_forget() -> None:
    """재연결 같은 경계에서는 같은 값도 다시 남겨야 한다."""
    edge = EdgeTrigger()
    edge.changed("stream", "up")
    edge.forget("stream")
    assert edge.changed("stream", "up") is True


def test_periodic_summary_emits_once_per_interval(clock: FakeClock) -> None:
    s = PeriodicSummary(interval_ms=1000)
    assert s.drain(clock.ms) is None, "첫 호출은 주기를 시작만 한다"
    for _ in range(15):
        s.count("frames")
    assert s.drain(clock.advance(999)) is None
    out = s.drain(clock.advance(1))
    assert out == {"frames": 15}


def test_periodic_summary_clears_between_intervals(clock: FakeClock) -> None:
    """**비우지 않으면 요약이 "지금까지 총합" 이 되어 구간 이상을 못 본다.**"""
    s = PeriodicSummary(interval_ms=1000)
    s.drain(clock.ms)
    s.count("drops", 2)
    assert s.drain(clock.advance(1000)) == {"drops": 2}
    s.count("drops", 3)
    assert s.drain(clock.advance(1000)) == {"drops": 3}


def test_periodic_summary_averages_observations(clock: FakeClock) -> None:
    s = PeriodicSummary(interval_ms=1000)
    s.drain(clock.ms)
    for value in (10.0, 20.0, 30.0):
        s.observe("infer_ms", value)
    assert s.drain(clock.advance(1000)) == {"infer_ms_avg": 20.0}


def test_periodic_summary_resyncs_when_far_behind(clock: FakeClock) -> None:
    """크게 밀렸으면 몰아 내지 않는다 — 커맨더의 주기 규칙과 같은 원칙이다."""
    s = PeriodicSummary(interval_ms=1000)
    s.drain(clock.ms)
    s.count("frames", 5)
    assert s.drain(clock.advance(10_000)) == {"frames": 5}
    s.count("frames", 1)
    assert s.drain(clock.advance(500)) is None, "재동기했으므로 아직 이르다"
    assert s.drain(clock.advance(500)) == {"frames": 1}


def test_formatter_keeps_unicode_readable() -> None:
    """한글 사유를 `\\uXXXX` 로 적으면 사람이 로그를 못 읽는다."""
    record = logging.LogRecord("mechadog.test", logging.WARNING, "", 0, "", (), None)
    record.event = "telemetry_discarded"
    record.detail = {"reason": "seq 역전·중복"}
    line = JsonlFormatter().format(record)
    assert "seq 역전·중복" in line
