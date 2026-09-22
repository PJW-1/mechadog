"""Selected-case execution, cancellation, and GUI automatic result wiring; no robot."""

import json
import socket
import threading
import time
from dataclasses import replace

import pytest

from host.telemetry.receiver import Reading
from tools import field_measure as fm
from tools import field_plan as plan
from tools import field_sessions as sessions


def case(case_id):
    return next(c for c in plan.catalog()["cases"] if c["id"] == case_id)


def samples():
    source = json.loads(
        (plan.ROOT / "tests/fixtures/telemetry_samples.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    base = Reading.of(source)
    return [fm.Sample(10 + i * 0.1, replace(base, seq=i + 1, pitch=float(i))) for i in range(10)]


def test_selected_case_controls_the_entire_sequence():
    assert sessions.route(case("HW-008")) == ("forward", "reverse")
    assert sessions.route(case("HW-009")) == ("left", "right", "reverse_turn")
    assert sessions.route(case("HW-002")) == ("imu",)
    assert sessions.route(case("TC-N-012")) == ()
    assert sessions.route(case("HW-016")) == ()
    assert sessions.route(case("HW-010")) == ("guided",)


def test_metrics_keep_zero_and_do_not_invent_physical_latency():
    result = sessions.metrics(samples())
    assert result["rate_hz"] == pytest.approx(10)
    assert result["pitch"]["min"] == 0
    assert result["pitch"]["mean"] == 4.5
    assert result["seq_missing_same_boot"] == 0
    assert "latency_ms" not in result
    with pytest.raises(ValueError):
        sessions.metrics([])


def test_gap_and_reboot_not_conflated():
    rows = samples()
    rows[4].reading = replace(rows[4].reading, seq=6)
    rows[5].reading = replace(rows[5].reading, boot_id="newboot", seq=1)
    result = sessions.metrics(rows)
    assert result["seq_missing_same_boot"] == 1
    assert len(result["boot_ids"]) == 2


def test_active_not_approved_opens_no_socket(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: pytest.fail("socket before approval"))
    runner = sessions.MeasurementSession("mechdog-02", "127.0.0.1", None, None, threading.Event())
    with pytest.raises(ValueError, match="준비"):
        runner.run(case("HW-008"))


def test_cancel_prevents_next_tick_and_prompt():
    cancelled = threading.Event()
    cancelled.set()
    guard = sessions.CommandGuard(fm.Commander(fm.CommandEncoder()), cancelled)
    with pytest.raises(sessions.MeasurementCancelledError):
        guard.tick(0)
    runner = sessions.MeasurementSession(
        "mechdog-02",
        "127.0.0.1",
        lambda _p: pytest.fail("prompt after cancellation"),
        None,
        cancelled,
    )
    with pytest.raises(sessions.MeasurementCancelledError):
        runner.prompt("test")


def test_gui_prompt_handler_is_scoped():
    token = fm.ASK_HANDLER.set(lambda _p: "-4")
    try:
        assert fm._ask_number("angle", signed=True) == -4
    finally:
        fm.ASK_HANDLER.reset(token)
    assert fm.ASK_HANDLER.get() is None


def test_passive_session_never_sends_and_saves_samples(monkeypatch):
    class Tap:
        latest = samples()[0].reading

        def __init__(self, *_a):
            self.closed = False

        def wait_flag(self, *_a, **_k):
            return True, 0

        def window(self, _s):
            return samples()

        def close(self):
            self.closed = True

    class Sock:
        def setblocking(self, _v):
            pass

        def sendto(self, *_a):
            pytest.fail("passive session sent a packet")

        def close(self):
            pass

    monkeypatch.setattr(fm, "TelemetryTap", Tap)
    monkeypatch.setattr(socket, "socket", lambda *_a: Sock())
    monkeypatch.setattr(sessions.MeasurementSession, "capture", lambda *_a: samples())
    updates = []
    runner = sessions.MeasurementSession(
        "mechdog-02", "127.0.0.1", None, updates.append, threading.Event()
    )
    result = runner.run(case("HW-002"))
    assert result[0].data["samples"] == 10
    assert result[0].data["telemetry_device_id"] == plan.identities()["mechdog-02"]
    assert isinstance(updates[-1], dict)


def test_active_cancel_always_sends_estop(monkeypatch):
    class Tap:
        latest = samples()[0].reading

        def __init__(self, *_a):
            pass

        def wait_flag(self, *_a, **_k):
            return True, 0

        def close(self):
            pass

    packets = []

    class Sock:
        def setblocking(self, _v):
            pass

        def sendto(self, packet, _peer):
            packets.append(json.loads(packet))

        def close(self):
            pass

    def cancelled(*_a):
        raise sessions.MeasurementCancelledError("cancel")

    monkeypatch.setattr(fm, "TelemetryTap", Tap)
    monkeypatch.setattr(socket, "socket", lambda *_a: Sock())
    monkeypatch.setattr(fm, "m_drive", cancelled)
    runner = sessions.MeasurementSession(
        "mechdog-02", "127.0.0.1", None, lambda _x: None, threading.Event()
    )
    with pytest.raises(sessions.MeasurementCancelledError):
        runner.run(case("HW-008"), approved=True)
    assert [p["type"] for p in packets] == ["STOP", "ESTOP"]


def test_movement_cases_lists_auto_floor_drive_in_catalog_order():
    ids = [c["id"] for c in sessions.movement_cases(plan.catalog()["cases"])]
    assert ids == [
        "HW-011",
        "TC-F-001",
        "HW-007",
        "HW-008",
        "HW-009",
        "TC-F-002",
        "TC-N-008",
    ]
    assert all(sessions.route(c) not in ((), ("guided",)) for c in
               sessions.movement_cases(plan.catalog()["cases"]))


def test_movement_batch_runs_each_case_and_saves(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    if os.environ.get("MECHADOG_GUI_TEST_ISOLATED") != "1":
        script = (
            "import runpy,sys,pytest;from pathlib import Path;"
            "tests=runpy.run_path('tests/test_field_sessions.py');"
            "tests['test_movement_batch_runs_each_case_and_saves']"
            "(Path(sys.argv[1]),pytest.MonkeyPatch())"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=plan.ROOT,
            env={**os.environ, "MECHADOG_GUI_TEST_ISOLATED": "1"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    import tkinter as tk

    from tools.field_measure_ui import MeasurementWindow

    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: pytest.fail("hardware in GUI test"))

    ran = []

    class FakeSession:
        def __init__(self, _device, _host, ask, notify, _cancel):
            self.ask, self.notify = ask, notify

        def run(self, selected, approved):
            ran.append((selected["id"], approved))
            self.notify(
                {
                    "result": {
                        "item": selected["id"],
                        "summary": f"{selected['id']} 실측 완료",
                        "verdict": "measured",
                    }
                }
            )

    root = tk.Tk()
    root.withdraw()
    env = {"operator": "test", "firmware": "mock", "host_revision": "test", "conditions": "mock"}
    window = MeasurementWindow(
        root,
        case("HW-008"),
        "mechdog-02",
        "127.0.0.1",
        tmp_path,
        env,
        plan.record,
        lambda: None,
        approved=True,
        session_factory=FakeSession,
        cases=[case("HW-008"), case("HW-009")],
    )
    window.window.withdraw()
    deadline = time.monotonic() + 3
    try:
        while not window.done and time.monotonic() < deadline:
            root.update()
            threading.Event().wait(0.01)
        assert window.done
        assert ran == [("HW-008", True), ("HW-009", True)]
        rows = plan.read_records(tmp_path, "mechdog-02")
        assert [r["case_id"] for r in rows] == ["HW-008", "HW-009"]
        assert all(r["status"] == "일부 측정" for r in rows)
        assert all("실측 완료" in r["notes"] for r in rows)
    finally:
        window.cancel.set()
        window.thread.join(timeout=1)
        root.destroy()


def test_measurement_window_prompt_completion_and_automatic_save(tmp_path, monkeypatch):
    # Tcl/Tk has process-global native state. Run this second GUI lifecycle in a
    # fresh interpreter, rather than sharing a destroyed Tk with the planner test.
    import os
    import subprocess
    import sys

    if os.environ.get("MECHADOG_GUI_TEST_ISOLATED") != "1":
        script = (
            "import runpy,sys,pytest;from pathlib import Path;"
            "tests=runpy.run_path('tests/test_field_sessions.py');"
            "tests['test_measurement_window_prompt_completion_and_automatic_save']"
            "(Path(sys.argv[1]),pytest.MonkeyPatch())"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=plan.ROOT,
            env={**os.environ, "MECHADOG_GUI_TEST_ISOLATED": "1"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    import tkinter as tk

    from tools.field_measure_ui import MeasurementWindow

    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: pytest.fail("hardware in GUI test"))

    class FakeSession:
        def __init__(self, _device, _host, ask, notify, _cancel):
            self.ask, self.notify = ask, notify

        def run(self, selected, approved):
            assert selected["id"] == "HW-008" and approved
            assert self.ask("이동 거리 mm") == "300"
            self.notify({"result": {"item": "전진", "summary": "100 mm/s", "verdict": "measured"}})

    root = tk.Tk()
    root.withdraw()
    env = {"operator": "test", "firmware": "mock", "host_revision": "test", "conditions": "mock"}
    window = MeasurementWindow(
        root,
        case("HW-008"),
        "mechdog-02",
        "127.0.0.1",
        tmp_path,
        env,
        plan.record,
        lambda: None,
        approved=True,
        session_factory=FakeSession,
    )
    window.window.withdraw()
    deadline = time.monotonic() + 3
    try:
        while not window.done and time.monotonic() < deadline:
            root.update()
            if window.pending:
                window.input.set("300")
                window.respond()
            threading.Event().wait(0.01)
        assert window.done
        rows = plan.read_records(tmp_path, "mechdog-02")
        assert len(rows) == 1
        assert rows[0]["case_id"] == "HW-008"
        assert rows[0]["status"] == "일부 측정"
        assert "100 mm/s" in rows[0]["notes"]
        assert rows[0]["evidence"]
    finally:
        window.cancel.set()
        window.thread.join(timeout=1)
        root.destroy()
