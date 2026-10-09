"""텔레메트리 진단 단독 검증 — `host.telemetry.telemetry_watch.TelemetryWatch` (Runtime 분할 13단계).

실제 수신·세션 개시와 맞물리는 시나리오는 `test_runtime.py` 가 `Runtime.ingest` 로 본다.
여기서는 런타임 없이 각속도 접기, 명령 폐기 경고의 지속 조건, 추종 중 막힘 엣지를 본다.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from host.common.logging_setup import PeriodicSummary
from host.telemetry.telemetry_watch import TelemetryWatch

CONFIG = {"safety": {"cmd_timeout_ms": 600}}


def _watch(
    *, tracking: bool = False, session_pending: bool = False
) -> tuple[TelemetryWatch, PeriodicSummary, Any]:
    behavior = SimpleNamespace(tracking=tracking, state="TRACK" if tracking else "PATROL")
    summary = PeriodicSummary(interval_ms=1000)
    watch = TelemetryWatch(
        CONFIG,
        behavior=behavior,  # type: ignore[arg-type]
        summary=summary,
        session_pending=lambda: session_pending,
    )
    return watch, summary, behavior


def _events(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, Any]]]:
    return [
        (r.event, r.detail)  # type: ignore[attr-defined]
        for r in caplog.records
        if getattr(r, "event", None) is not None
    ]


def test_yaw_rate_folds_across_north_and_skips_gaps() -> None:
    watch, summary, _ = _watch()
    summary.drain(0)
    watch.observe_yaw_rate(None, 0)
    watch.observe_yaw_rate(359.0, 0)
    watch.observe_yaw_rate(1.0, 100)  # +2° / 0.1s — 경계를 넘어도 −358° 가 아니다
    watch.observe_yaw_rate(11.0, 1200)  # 1초를 넘는 공백은 재지 않는다
    assert summary.drain(1000) == {"yaw_rate_deg_s_avg": 20.0}


def test_commands_ignored_needs_a_sustained_stale_age(caplog: pytest.LogCaptureFixture) -> None:
    watch, _, _ = _watch()
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        watch.watch_command_uptake(9000, 0)
        watch.watch_command_uptake(9000, 999)
        assert _events(caplog) == []
        watch.watch_command_uptake(40, 1000)  # 한 번 정상이면 처음부터 다시 잰다
        watch.watch_command_uptake(9000, 1100)
        watch.watch_command_uptake(9000, 2100)
    assert _events(caplog) == [
        (
            "commands_ignored",
            {"last_cmd_age_ms": 9000, "hint": "세션 개시 실패 가능 (PROTOCOL 4절)"},
        )
    ]


def test_commands_are_not_judged_before_the_session_opens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watch, _, _ = _watch(session_pending=True)
    with caplog.at_level(logging.WARNING, logger="mechadog.runtime"):
        for at in range(0, 3000, 100):
            watch.watch_command_uptake(9000, at)
    assert _events(caplog) == []


def test_track_blocked_logs_each_edge_once(caplog: pytest.LogCaptureFixture) -> None:
    watch, _, behavior = _watch(tracking=True)
    with caplog.at_level(logging.INFO, logger="mechadog.runtime"):
        watch.watch_track_blocked(SimpleNamespace(obstacle=False, dist_cm=80), 0)
        watch.watch_track_blocked(SimpleNamespace(obstacle=True, dist_cm=7), 100)
        watch.watch_track_blocked(SimpleNamespace(obstacle=True, dist_cm=6), 200)
        behavior.tracking = False  # 추종을 벗어나면 막힘 구간이 끝난다
        watch.watch_track_blocked(SimpleNamespace(obstacle=True, dist_cm=6), 900)
    assert _events(caplog) == [
        ("track_blocked", {"state": "TRACK", "dist_cm": 7}),
        ("track_unblocked", {"state": "TRACK", "blocked_ms": 800}),
    ]
