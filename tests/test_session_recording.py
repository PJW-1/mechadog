"""실기 세션 동시 기록과 보행 잠금 (`host/telemetry/session_recorder.py` · `Runtime --record-dir --motion-lock`).

실기 없이 닫힌다 — 가짜 소켓·가짜 시계로 런타임을 돌리고 기록 파일을 읽는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeClock
from fastapi.testclient import TestClient
from test_runtime import DEVICE, PEER, FakeSocket, telemetry
from test_runtime_lidar import _runtime

from host.behavior.fsm import Event
from host.common.protocol import TelemetryEncoder
from host.dashboard.server import create_app
from host.dashboard.state import DashboardState
from host.runtime import MOTION_LOCK_TYPES, Runtime, build_parser, dashboard_wiring, main
from host.telemetry.session_recorder import SessionRecorder


@pytest.fixture
def config(cfg: dict) -> dict:
    """`test_runtime.config` 와 같다 — 주소는 `network` 절 안에."""
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], mechdog_ip=PEER[0])
    merged["auth"] = dict(cfg["auth"], require_both=False)
    return merged


def _events(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


# ── 기록기 ───────────────────────────────────────────────────
def test_recorder_writes_events_in_order_with_manifest(tmp_path: Path) -> None:
    ticks = iter(range(100, 200))
    recorder = SessionRecorder(tmp_path, manifest={"device": "x"}, clock=lambda: next(ticks))
    recorder.start()
    recorder.record("a", n=1)
    recorder.record_raw("scan", b'{"type":"SCAN"}', at_ms=5)
    recorder.record_raw("scan", b"\xff\x00", at_ms=6)
    summary = recorder.close()
    events = _events(tmp_path)
    assert [e["kind"] for e in events] == ["a", "scan", "scan"]
    assert events[1] == {"t": 5, "kind": "scan", "raw": '{"type":"SCAN"}'}
    assert events[2]["raw_b64"] == "/wA=", "규약 밖 바이트도 버리지 않는다"
    assert summary["written"] == 3 and summary["dropped"] == 0 and summary["failed"] is None
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["device"] == "x"
    # 시계 호출 순서: 생성자(manifest)=100, record("a")=101, close→summary=102. record_raw 는 at_ms 를 쓴다.
    assert manifest["started_clock_ms"] == 100
    assert events[0]["t"] == 101
    assert summary["stopped_clock_ms"] == 102
    assert "stopped_mono_ms" not in summary
    assert "started_mono_ms" not in manifest
    assert manifest["clock_domain"] == "host system_clock_ms (epoch wall clock)"
    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["written"] == 3 and saved["stopped_clock_ms"] == 102


def test_full_queue_drops_and_reports_unhealthy(tmp_path: Path) -> None:
    """기록이 밀려도 제어 쪽은 막히지 않는다 — 버린 수를 세고 «건강하지 않음» 이 된다."""
    recorder = SessionRecorder(tmp_path, max_queue=2)  # 스레드를 아직 시작하지 않았다
    for i in range(5):
        recorder.record("x", i=i)
    assert recorder.dropped == 3
    assert not recorder.healthy


def test_frame_is_saved_with_hash(tmp_path: Path) -> None:
    recorder = SessionRecorder(tmp_path)
    recorder.start()
    name = recorder.save_frame(b"\xff\xd8jpeg", frame_seq=7, received_ms=1000)
    recorder.close()
    assert (tmp_path / name).read_bytes() == b"\xff\xd8jpeg"
    frame = [e for e in _events(tmp_path) if e["kind"] == "camera_frame"][0]
    assert frame["frame_seq"] == 7 and len(frame["sha256"]) == 64


# ── 런타임 ───────────────────────────────────────────────────
def test_runtime_records_telemetry_and_sent_commands(
    config: dict, clock: FakeClock, tmp_path: Path
) -> None:
    recorder = SessionRecorder(tmp_path, clock=lambda: clock.ms)
    recorder.start()
    r = Runtime(config, device_id=DEVICE, clock=clock, recorder=recorder)
    raw = telemetry(TelemetryEncoder(DEVICE, "boot-1"), state="IDLE")
    sock = FakeSocket(clock, inbox=[(150, raw)])
    r.serve(sock, duration_s=0.5, clock=clock)
    recorder.close()
    events = _events(tmp_path)
    kinds = {e["kind"] for e in events}
    assert {"telemetry", "command_sent"} <= kinds
    tlm = [e for e in events if e["kind"] == "telemetry"][0]
    assert tlm["raw"] == raw.decode() and tlm["accepted"] is True and isinstance(tlm["t"], int)
    sent_lines = [line for e in events if e["kind"] == "command_sent" for line in e["lines"]]
    sent_on_wire = [payload.decode() for _at, payload in sock.sent]
    assert sorted(set(sent_lines)) == sorted(set(sent_on_wire)), "기록 = 실제로 나간 전문"


def test_motion_lock_never_sends_motion(config: dict, clock: FakeClock, tmp_path: Path) -> None:
    """보행 잠금이면 순찰 요청도 거절하고, 어떤 경로로도 MOVE 가 소켓에 닿지 않는다."""
    recorder = SessionRecorder(tmp_path, clock=lambda: clock.ms)
    recorder.start()
    r = Runtime(config, device_id=DEVICE, clock=clock, recorder=recorder, motion_lock=True)
    enc = TelemetryEncoder(DEVICE, "boot-1")
    inbox = [(100 + 100 * i, telemetry(enc, state="IDLE")) for i in range(10)]
    sock = FakeSocket(clock, inbox=inbox)
    r.begin(sock)
    assert r.start_patrol(clock.ms) is False
    # 관제 즉시 송신 경로로 MOVE 를 넣어도 막힌다
    r.send_immediate(json.dumps({"type": "MOVE", "seq": 999, "ts": 1, "step": 50, "angle": 0}))
    r._shutdown(sock)
    recorder.close()
    sent_types = set(sock.types())
    assert sent_types <= MOTION_LOCK_TYPES
    assert "ESTOP" in sent_types, "종료 정지는 그대로 나간다"
    blocked = [e for e in _events(tmp_path) if e["kind"] == "command_blocked"]
    assert blocked and json.loads(blocked[0]["lines"][0])["type"] == "MOVE"


def test_motion_lock_refuses_patrol_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--device", DEVICE, "--motion-lock", "--patrol"])
    assert exc.value.code == 2
    assert "motion-lock" in capsys.readouterr().err


def test_motion_lock_rejects_goto_without_reservation_but_allows_point_hint(config, clock):
    runtime, navigator = _runtime(config, clock, motion_lock=True)
    navigator.observe_map_pose((2.0, 2.0, 0.0), clock.ms)
    runtime.begin(FakeSocket(clock))
    try:
        app = create_app(
            DashboardState(DEVICE, stale_after_ms=3000),
            **dashboard_wiring(runtime, config, vision=None, blackbox=None),
        )
        with TestClient(app) as client:
            response = client.post("/api/command/goto", json={"x": 4.0, "y": 1.0})
            assert response.status_code == 200
            assert response.json()["accepted"] is False
            assert response.json()["detail"] == "보행 잠금 중 — 이동할 수 없다"
            assert runtime._goto_asked is None
            assert runtime._route_asked is None
            assert not runtime._patrol_asked
            runtime.tick(clock.ms)
            feedback = client.get("/api/nav").json()["goal_feedback"]
            assert feedback["accepted"] is False
            assert feedback["detail"] == response.json()["detail"]
            assert navigator.goal is None
            assert runtime.behavior.state == "IDLE"
            hint = client.post("/api/command/locate", json={"x": 2.0, "y": 2.0})
            assert hint.json()["accepted"] is True
            runtime.tick(clock.ms)
            assert navigator._point_hint == (2.0, 2.0, clock.ms)
            assert runtime._goto_asked is None
    finally:
        runtime.release()


def test_record_flags_parse() -> None:
    args = build_parser().parse_args(
        [
            "--device",
            DEVICE,
            "--record-dir",
            "x",
            "--motion-lock",
            "--record-frame-ms",
            "500",
            "--maps",
            "m",
        ]
    )
    assert args.record_dir == "x" and args.motion_lock and args.record_frame_ms == 500
    assert args.maps == "m"


def test_motion_lock_type_set_has_no_motion() -> None:
    assert not {"MOVE", "POSE", "GAIT", "ACTION", "RESET_SAFE"} & MOTION_LOCK_TYPES
    assert Event.START_PATROL  # 순찰 사건 이름이 바뀌면 잠금 관문도 바꿔야 한다


def test_session_summary_reads_a_recording() -> None:
    """요약 도구가 흐름별 수·최대 공백·IMU·측위 이동을 낸다 (`tools/ops/session_summary.py`)."""
    from tools.ops.session_summary import summarize

    tlm = '{"state":"IDLE","imu":{"pitch":0,"roll":0,"yaw":%s}}'
    events = [
        {"t": 0, "kind": "telemetry", "raw": tlm % 10.0, "accepted": True},
        {"t": 100, "kind": "telemetry", "raw": tlm % 12.5, "accepted": True},
        {"t": 400, "kind": "telemetry", "raw": tlm % 15.0, "accepted": True},
        {"t": 50, "kind": "localization", "updated": True, "points": 500, "pose": [0, 0, 0]},
        {"t": 150, "kind": "localization", "updated": True, "points": 480, "pose": [0.3, 0.4, 0]},
        {"t": 60, "kind": "command_sent", "lines": ['{"type":"STOP"}', '{"type":"MOVE"}']},
        {"t": 70, "kind": "command_blocked", "lines": ['{"type":"MOVE"}']},
    ]
    report = summarize(events)
    assert report["streams"]["telemetry"]["max_gap_ms"] == 300
    assert report["imu_yaw"]["first_deg"] == 10.0 and report["imu_yaw"]["last_deg"] == 15.0
    assert report["localization"]["travel_m"] == pytest.approx(0.5)
    assert report["commands_sent"] == {"STOP": 1, "MOVE": 1}
    assert report["commands_blocked"] == 1
