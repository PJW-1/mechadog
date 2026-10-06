"""기록 세션 재생 (`tools/ops/replay_session.py`).

시험 안에서 기록기로 작은 합성 세션을 만들고, 그것을 다시 넣어 같은 판단이 나오는지 본다.
실기·소켓 없이 닫힌다 — 가짜 시계와 가짜 소켓뿐이다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeClock
from test_runtime import DEVICE, PEER, FakeSocket, telemetry, vision_result

from host.behavior.fsm import Event
from host.common.protocol import TelemetryEncoder
from host.runtime import Runtime
from host.telemetry.session_recorder import SessionRecorder
from host.vision.vlm_reader import VlmReader
from tools.ops.replay_session import (
    RecordedVision,
    compare,
    load_session,
    main,
    normalize_command,
    replay,
    vision_from_event,
)


@pytest.fixture
def config(cfg: dict) -> dict:
    merged = dict(cfg)
    merged["network"] = dict(cfg["network"], mechdog_ip=PEER[0])
    merged["auth"] = dict(cfg["auth"], require_both=False)
    return merged


class ScriptedSocket(FakeSocket):
    """정해진 시각에 관제 입력을 넣는다 — 대시보드 스레드가 누른 버튼 대신이다."""

    def __init__(self, clock: FakeClock, inbox, actions: list[tuple[int, Callable[[], object]]]):
        super().__init__(clock, inbox)
        self.actions = sorted(actions, key=lambda a: a[0])

    def recvfrom(self, size: int):
        try:
            return super().recvfrom(size)
        finally:
            while self.actions and self.actions[0][0] <= self.clock.ms:
                self.actions.pop(0)[1]()


def _record_session(config: dict, clock: FakeClock, directory: Path) -> None:
    """순찰 → 사람 발견(ALERT) → 관제 ESTOP 이 들어간 3초짜리 세션을 기록한다."""
    start = clock.ms
    recorder = SessionRecorder(directory, manifest={"argv": ["--device", DEVICE]}, clock=clock)
    recorder.start()
    seen = start + 1200
    vision = RecordedVision(
        [
            (start + 50, vision_result(1, start + 50, present=False, hits=0, last_seen_ms=None)),
            (seen, vision_result(2, seen, present=True, hits=3, last_seen_ms=seen)),
            (
                seen + 100,
                vision_result(3, seen + 100, present=True, hits=3, last_seen_ms=seen + 100),
            ),
        ],
        clock=clock,
        stall_ms=int(config["vision"]["stall_timeout_ms"]),
    )
    runtime = Runtime(
        config,
        device_id=DEVICE,
        clock=clock,
        vision=vision,
        vlm_reader=VlmReader(None),
        recorder=recorder,
    )
    runtime.vlm.start = lambda: None  # type: ignore[method-assign]  # 적재 스레드 경합을 없앤다
    enc = TelemetryEncoder(DEVICE, "boot-1")
    inbox = [(start + 100 * i + 7, telemetry(enc, state="IDLE")) for i in range(30)]
    actions = [(start + 2500, lambda: runtime.apply_external(Event.ESTOP))]
    sock = ScriptedSocket(clock, inbox, actions)
    runtime.ask_patrol()
    runtime.serve(sock, duration_s=3.0, clock=clock)
    recorder.close()


@pytest.fixture
def session(config: dict, clock: FakeClock, tmp_path: Path) -> Path:
    _record_session(config, clock, tmp_path / "rec")
    return tmp_path / "rec"


def _commands(events: list[dict]) -> list[str]:
    return [
        normalize_command(line)
        for e in events
        if e["kind"] == "command_sent"
        for line in e["lines"]
    ]


def _fsm(events: list[dict]) -> list[tuple]:
    return [(e["previous"], e["state"], e["trigger"]) for e in events if e["kind"] == "fsm"]


def test_recording_has_the_inputs_replay_needs(session: Path) -> None:
    """관제 입력·기동 시각·영상 판정 전체·경보 단계가 기록에 남는다."""
    _manifest, events = load_session(session)
    kinds = {e["kind"] for e in events}
    assert {"runtime_begin", "operator", "vision", "escalation", "fsm"} <= kinds
    actions = [e["action"] for e in events if e["kind"] == "operator"]
    assert actions == ["ask_patrol", "apply_external"]
    vision = [e for e in events if e["kind"] == "vision"]
    assert all("sighting" in e and "tracks" in e and "fallen" in e for e in vision)


def test_roundtrip_reproduces_recorded_decisions(config: dict, session: Path) -> None:
    """기록한 세션을 다시 넣으면 같은 명령·같은 FSM 전이·같은 경보 단계가 나온다."""
    _manifest, recorded = load_session(session)
    replayed = replay(recorded, config, device_id=DEVICE)
    assert _commands(replayed) == _commands(recorded)
    assert _fsm(replayed) == _fsm(recorded)
    assert ("PATROL", "ALERT", "PERSON_FOUND") in _fsm(recorded), "시나리오가 판단을 담는다"
    levels = [e["level"] for e in replayed if e["kind"] == "escalation"]
    assert levels == [e["level"] for e in recorded if e["kind"] == "escalation"]
    report = compare(recorded, replayed)
    assert report["commands"]["match_ratio"] == 1.0
    assert report["commands"]["first_divergence"] is None
    assert report["fsm"]["match_ratio"] == 1.0


def test_replay_is_deterministic(config: dict, session: Path) -> None:
    _manifest, recorded = load_session(session)
    first = replay(recorded, config, device_id=DEVICE)
    second = replay(recorded, config, device_id=DEVICE)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_what_if_config_changes_the_decision(config: dict, session: Path) -> None:
    """설정을 바꿔 다시 넣으면 판단이 달라지고, 비교가 그 차이를 짚는다."""
    _manifest, recorded = load_session(session)
    changed = dict(config)
    changed["network"] = dict(config["network"], cmd_rate_hz=5)
    replayed = replay(recorded, changed, device_id=DEVICE)
    report = compare(recorded, replayed)
    assert report["commands"]["match_ratio"] < 1.0
    assert report["commands"]["first_divergence"] is not None


def test_vision_event_roundtrips_to_the_same_result() -> None:
    original = vision_result(5, 1000, present=True, hits=3, last_seen_ms=990)
    event = {
        "t": 1010,
        "kind": "vision",
        "frame_seq": original.frame_seq,
        "frame_received_ms": original.frame_received_ms,
        "completed_ms": original.completed_ms,
        "inference_ms": original.inference_ms,
        "size": [original.frame_width, original.frame_height],
        "detections": [{"label": "person", "score": 0.9, "box": list(original.detections[0].box)}],
        "person_present": True,
        "sighting": {
            "present": True,
            "changed": True,
            "hits": 3,
            "best_score": 0.9,
            "last_seen_ms": 990,
            "box": list(original.sighting.box or ()),
        },
        "tracks": [
            {"track_id": 1, "box": list(original.tracks[0].box), "score": 0.9, "last_seen_ms": 1000}
        ],
        "fallen": {
            "fallen": False,
            "changed": False,
            "candidate": False,
            "aspect": None,
            "still_ms": 0,
        },
        "markers": [],
        "ppe": None,
        "hazard": None,
    }
    rebuilt, exact = vision_from_event(event)
    assert exact
    assert rebuilt.sighting == original.sighting
    assert rebuilt.tracks == original.tracks
    assert rebuilt.fallen == original.fallen


def test_old_vision_event_is_approximated() -> None:
    """판정 전체가 없는 옛 기록은 검출로 근사하고 «근사» 라고 알린다."""
    event = {
        "t": 500,
        "kind": "vision",
        "frame_seq": 1,
        "frame_received_ms": 480,
        "completed_ms": 490,
        "inference_ms": 5.0,
        "size": [640, 480],
        "detections": [{"label": "person", "score": 0.8, "box": [1, 2, 3, 4]}],
        "person_present": True,
    }
    rebuilt, exact = vision_from_event(event)
    assert not exact
    assert rebuilt.sighting.present and rebuilt.sighting.hits == 1


def test_compare_points_at_first_divergence() -> None:
    def sent(t: int, *types: str) -> dict:
        return {"t": t, "kind": "command_sent", "lines": [json.dumps({"type": x}) for x in types]}

    recorded = [sent(0, "STOP"), sent(100, "MOVE"), sent(200, "ESTOP")]
    replayed = [sent(0, "STOP"), sent(100, "STOP"), sent(200, "ESTOP")]
    report = compare(recorded, replayed)
    assert report["commands"]["recorded"] == 3 and report["commands"]["matched"] == 2
    first = report["commands"]["first_divergence"]
    assert first["recorded"] == ['{"type":"MOVE"}'] and first["replayed"] == ['{"type":"STOP"}']
    assert first["t_ms"] == 100


def test_normalize_command_drops_seq_and_ts() -> None:
    line = json.dumps({"type": "MOVE", "seq": 9, "ts": 123, "step": 50, "angle": 0})
    assert normalize_command(line) == '{"angle":0,"step":50,"type":"MOVE"}'


def test_cli_writes_report(
    session: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict, capsys
) -> None:
    monkeypatch.setattr("tools.ops.replay_session.load_config", lambda _device: config)
    out = tmp_path / "report.json"
    assert main([str(session), "--json", str(out), "--set", "network.cmd_rate_hz=5"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["baseline"]["commands"]["match_ratio"] == 1.0
    assert report["what_if"]["commands"]["match_ratio"] < 1.0
    assert report["overrides"] == {"network.cmd_rate_hz": 5}
    text = capsys.readouterr().out
    assert "명령 일치" in text
