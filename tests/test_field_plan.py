"""Offline evidence isolation and measurement collector safety regressions."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from tools import field_measure as fm
from tools import field_plan as plan

requires_display = pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="tkinter GUI needs a display",
)


@pytest.fixture
def environment():
    return {
        "operator": "tester",
        "firmware": "installed-test-fw",
        "host_revision": "test-host",
        "conditions": "test floor, 8V, no load",
    }


def test_device_records_are_separate_and_evidence_preserved(tmp_path, environment):
    original = tmp_path / "raw.csv"
    original.write_bytes(b"time,pitch\n0,0\n")
    first = plan.record(
        tmp_path, "mechdog-02", "HW-002", "통과", "angle verified", environment, [original]
    )
    second = plan.record(
        tmp_path, "mechdog-02", "HW-002", "일부 측정", "repeat", environment, [original]
    )
    assert first != second
    assert original.read_bytes() == (first.parent / "evidence/1/raw.csv").read_bytes()
    assert len(plan.read_records(tmp_path, "mechdog-02")) == 2
    assert plan.read_records(tmp_path, "mechdog-01") == []
    text = plan.export(tmp_path, "mechdog-02").read_text(encoding="utf-8")
    assert "상태: 일부 측정" in text
    assert "179주기" in text


def test_pass_requires_evidence_and_environment(tmp_path, environment):
    with pytest.raises(ValueError, match="증거"):
        plan.record(tmp_path, "mechdog-02", "HW-002", "통과", "observed", environment, [])
    with pytest.raises(ValueError, match="펌웨어"):
        plan.record(tmp_path, "mechdog-02", "HW-002", "일부 측정", "observed", {}, [])


@pytest.mark.parametrize("device,actual", [("mechdog-01", None), ("mechdog-02", "another-board")])
def test_import_rejects_other_board(tmp_path, environment, device, actual):
    raw = tmp_path / "field.json"
    raw.write_text(
        json.dumps(
            {
                "tool": "field_measure",
                "device": device,
                "results": [{"data": {"telemetry_device_id": actual}}],
            }
        )
    )
    with pytest.raises(ValueError):
        plan.record(tmp_path, "mechdog-02", "HW-002", "일부 측정", "import", environment, [raw])


def test_legacy_unidentified_import_cannot_be_pass(tmp_path, environment):
    raw = tmp_path / "field.json"
    raw.write_text(
        json.dumps({"tool": "field_measure", "device": "mechdog-02", "results": [{"data": {}}]})
    )
    with pytest.raises(ValueError, match="구형"):
        plan.record(tmp_path, "mechdog-02", "HW-002", "통과", "import", environment, [raw])
    plan.record(
        tmp_path, "mechdog-02", "HW-002", "일부 측정", "identity pending", environment, [raw]
    )


def test_catalog_has_unique_ids_and_retains_all_requirements():
    cases = plan.catalog()["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) == 90
    assert len([i for i in ids if i.startswith("TC-F")]) == 51
    assert len([i for i in ids if i.startswith("TC-N")]) == 20
    assert next(c for c in cases if c["id"] == "TC-N-012")["phase"] == "제외"
    assert "FSM/PERSON_DOWN/L3" in next(c for c in cases if c["id"] == "HW-015")["criteria"]


def test_stored_identity_mismatch_is_not_silently_loaded(tmp_path, environment):
    path = plan.record(tmp_path, "mechdog-02", "HW-002", "일부 측정", "note", environment, [])
    row = json.loads(path.read_text(encoding="utf-8"))
    row["telemetry_device_id"] = "other-board"
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="식별자"):
        plan.read_records(tmp_path, "mechdog-02")


@pytest.mark.parametrize("item", [None, "1", "7", "10", "2"])
def test_help_invalid_experiments_and_unapproved_motion_send_nothing(monkeypatch, item):
    def forbidden(*_a, **_k):
        pytest.fail("network opened before approval")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sys, "argv", ["field_measure"] + (["--item", item] if item else []))
    if item == "2":
        with pytest.raises(SystemExit):
            fm.main()
    else:
        assert fm.main() == 0


def test_busy_port_fails_before_command_socket(monkeypatch):
    def busy(*_a, **_k):
        raise OSError("busy")

    def forbidden(*_a, **_k):
        pytest.fail("command socket opened on bind failure")

    monkeypatch.setattr(sys, "argv", ["field_measure", "--item", "0", "--host", "127.0.0.1"])
    monkeypatch.setattr(fm, "TelemetryTap", busy)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert fm.main() == 2


def test_receiver_rejects_alias_duplicate_malformed_and_stale():
    sample = json.loads(
        (Path(__file__).parent / "fixtures/telemetry_samples.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    tap = fm.TelemetryTap(0, sample["device_id"])
    try:
        wrong = {**sample, "device_id": "wrong", "device": sample["device_id"]}
        assert not tap.accept(json.dumps(wrong).encode())
        assert not tap.accept(b"{}")
        assert tap.accept(json.dumps(sample).encode())
        assert not tap.accept(json.dumps(sample).encode())
        assert tap.received == 1
        # Existing cached reading must not satisfy a newly issued command.
        assert not tap.wait_flag(lambda _r: True, timeout_s=0.01)[0]
    finally:
        tap.close()
    assert not tap._thread.is_alive()


def test_all_skipped_directions_never_pass(monkeypatch):
    monkeypatch.setattr(fm, "_ask", lambda _p: "s")
    result = fm.m_teleop(fm.Ctx(None, ("127.0.0.1", 1), None, None, "test", "127.0.0.1"))
    assert result.verdict == "skipped"


def test_nonfinite_input_rejected_but_signed_angles_kept(monkeypatch):
    answers = iter(["nan", "inf", "-20"])
    monkeypatch.setattr(fm, "_ask", lambda _p: next(answers))
    assert fm._ask_number("angle", signed=True) == -20


@requires_display
def test_gui_loads_without_network_or_measurement(monkeypatch, tmp_path):
    import tkinter as tk

    def forbidden(*_a, **_k):
        pytest.fail("offline GUI attempted network")

    def inspect_and_close(root):
        root.withdraw()
        root.update_idletasks()
        assert "MechaDog" in root.title()
        assert len(root.winfo_children()) >= 3
        root.destroy()

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(tk.Tk, "mainloop", inspect_and_close)
    plan.gui(tmp_path, "mechdog-02")


def test_history_spans_archive_days_but_stays_per_device(tmp_path, environment):
    archive = tmp_path / "05_실물_측정결과"
    yesterday = archive / "2026-09-19" / "기체별_실측"
    today = archive / "2026-09-20" / "기체별_실측"
    plan.record(yesterday, "mechdog-02", "HW-002", "일부 측정", "yesterday", environment, [])
    plan.record(today, "mechdog-02", "HW-002", "일부 측정", "today", environment, [])
    assert len(plan.read_records(today, "mechdog-02")) == 2
    assert plan.read_records(today, "mechdog-01") == []
