"""실기 세션 요약 도구의 입출력 (`tools/ops/session_summary.py`).

`test_session_recording.py` 가 `summarize` 의 계산을 본다면, 여기는 파일을 읽고 쓰는 쪽
(`load_events`·`main`·`draw`)과 빈 기록의 경계를 본다. 실기 없이 임시 폴더로 닫힌다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from host.slam.occupancy import MapMeta, OccupancyGrid
from tools.ops import session_summary as ss

EVENTS = [
    {"t": 1000, "kind": "telemetry", "raw": '{"state":"IDLE","imu":{"yaw":5.0}}', "accepted": True},
    {"t": 1200, "kind": "telemetry", "raw": "not json", "accepted": True},
    {"t": 1300, "kind": "telemetry", "raw": '{"state":"X"}', "accepted": False},
    {"t": 1100, "kind": "localization", "updated": True, "points": 400, "pose": [0, 0, 0]},
    {"t": 1500, "kind": "localization", "updated": False, "points": 10, "pose": [9, 9, 9]},
    {"t": 1600, "kind": "command_sent", "lines": ['{"type":"STOP"}', "garbage"]},
    {"t": 1700, "kind": "fsm", "previous": "IDLE", "state": "PATROL"},
    {"t": 1800, "kind": "navigator_phase", "phase": "halt", "halt_reason": "obstacle"},
]


def _write_events(directory: Path, events: list[dict]) -> None:
    lines = [json.dumps(e) for e in events]
    lines.insert(1, "")  # 끊긴 기록의 빈 줄은 건너뛴다
    (directory / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_events_skips_blank_lines(tmp_path: Path) -> None:
    _write_events(tmp_path, EVENTS)
    assert ss.load_events(tmp_path) == EVENTS


def test_gaps_needs_two_samples_and_a_positive_span() -> None:
    assert ss._gaps([]) == (None, None)
    assert ss._gaps([5]) == (None, None)
    assert ss._gaps([5, 5]) == (None, 0)  # 같은 시각 둘 — 주기를 구할 수 없다
    rate, gap = ss._gaps([0, 100, 400])
    assert rate == pytest.approx(2 / 0.4)
    assert gap == 300


def test_summarize_empty_recording_has_no_rates() -> None:
    """사건이 없어도 죽지 않고, 값 없음(None)·0 으로 답한다."""
    report = ss.summarize([])
    assert report["events"] == 0 and report["duration_s"] == 0
    assert report["streams"]["scan"] == {
        "count": 0,
        "hz": None,
        "max_gap_ms": None,
        "first_ms": None,
        "last_ms": None,
    }
    assert report["imu_yaw"] is None
    assert report["localization"]["updated_ratio"] is None
    assert report["localization"]["first_pose"] is None


def test_summarize_tolerates_bad_telemetry_and_bad_command_lines() -> None:
    """깨진 텔레메트리 원문·명령 줄은 세지 않거나 `?` 로 센다. 거부된 텔레메트리는 제외."""
    report = ss.summarize(EVENTS)
    assert report["telemetry_accepted"] == 2
    assert report["robot_states"] == {"IDLE": 1}
    assert report["imu_yaw"]["samples"] == 1
    assert report["commands_sent"] == {"STOP": 1, "?": 1}
    assert report["localization"]["attempts"] == 2
    assert report["localization"]["updated"] == 1
    assert report["localization"]["updated_ratio"] == 0.5
    assert report["fsm"] == [{"t_ms": 700, "from": "IDLE", "to": "PATROL"}]
    assert report["navigator_phases"] == [{"t_ms": 800, "phase": "halt", "reason": "obstacle"}]


def test_main_writes_report_and_includes_recorder_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`session_report.json` 을 남기고, 기록기 `summary.json` 의 버림·실패를 함께 싣는다."""
    _write_events(tmp_path, EVENTS)
    (tmp_path / "summary.json").write_text(
        json.dumps({"written": 8, "dropped": 1, "max_backlog": 3, "failed": 0, "extra": 1}),
        encoding="utf-8",
    )
    assert ss.main([str(tmp_path)]) == 0
    report = json.loads((tmp_path / "session_report.json").read_text(encoding="utf-8"))
    assert report["recorder"] == {"written": 8, "dropped": 1, "max_backlog": 3, "failed": 0}
    assert report["events"] == len(EVENTS)
    assert json.loads(capsys.readouterr().out) == report
    assert not (tmp_path / "trajectory.png").exists()  # --map 이 없으면 그림을 그리지 않는다


def test_main_without_recorder_summary_has_no_recorder_key(tmp_path: Path) -> None:
    _write_events(tmp_path, EVENTS)
    assert ss.main([str(tmp_path)]) == 0
    report = json.loads((tmp_path / "session_report.json").read_text(encoding="utf-8"))
    assert "recorder" not in report


def test_main_draws_trajectory_png_on_the_map(tmp_path: Path) -> None:
    """`--map` 이 있으면 측위 궤적을 지도 위에 그려 PNG 로 남긴다."""
    pytest.importorskip("matplotlib")
    _write_events(tmp_path, EVENTS)
    maps = tmp_path / "map"
    cells = np.zeros((20, 20), dtype=np.float32)
    cells[0, :] = 4.0
    cells[5:10, 5:10] = -2.0
    OccupancyGrid(MapMeta(0.1, -1.0, -1.0, 20, 20), cells).save(maps)
    out = tmp_path / "t.png"
    assert ss.main([str(tmp_path), "--map", str(maps), "--png", str(out)]) == 0
    assert out.read_bytes().startswith(b"\x89PNG")


def test_draw_without_localization_still_writes_a_map_image(tmp_path: Path) -> None:
    """측위가 한 번도 없으면 궤적 없이 지도만 그린다."""
    pytest.importorskip("matplotlib")
    maps = tmp_path / "map"
    OccupancyGrid(MapMeta(0.1, 0.0, 0.0, 10, 10)).save(maps)
    out = tmp_path / "empty.png"
    ss.draw([{"kind": "scan", "t": 0}], maps, out)
    assert out.read_bytes().startswith(b"\x89PNG")
