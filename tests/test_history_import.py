"""블랙박스 → 이력 DB 가져오기 (`tools/ops/history_import.py`, WBS 4.6.6 · ADR-46)."""

import json
import sqlite3
from pathlib import Path

import pytest

from host.common.history import HistoryStore
from tools.ops import history_import


def _entry(root: Path, name: str, meta: dict | str, snapshot: bool = False) -> None:
    folder = root / name
    folder.mkdir()
    text = meta if isinstance(meta, str) else json.dumps(meta)
    (folder / "meta.json").write_text(text, encoding="utf-8")
    if snapshot:
        (folder / "snapshot.jpg").write_bytes(b"jpeg")


@pytest.fixture
def blackbox(tmp_path: Path) -> Path:
    root = tmp_path / "blackbox"
    root.mkdir()
    _entry(root, "1000_fall", {"ts_ms": 1000, "event": "fall", "state": "ALERT"}, snapshot=True)
    _entry(
        root,
        "2000_ppe",
        {"ts_ms": 2000, "event": "ppe", "judgement": {"zone": "B"}, "escalation": "warning"},
    )
    _entry(root, "3000_bad", "{not json")
    _entry(root, "4000_nokeys", {"event": "x"})
    _entry(root, "notes", {"ts_ms": 5000, "event": "x"})
    return root


def run(blackbox: Path, db: Path) -> int:
    return history_import.main(
        ["--device", "mechdog-02", "--blackbox", str(blackbox), "--db", str(db)]
    )


def test_import_inserts_valid_entries_and_is_idempotent(blackbox, tmp_path, capsys):
    db = tmp_path / "h.sqlite3"
    assert run(blackbox, db) == 0
    assert "inserted 2" in capsys.readouterr().out
    store = HistoryStore(db)
    try:
        assert store.incidents()[1] == 2
        fall = store.incident("mechdog-02_1000_fall")
        assert fall["robot_id"] == "mechdog-02"
        assert fall["snapshot_path"] == "1000_fall/snapshot.jpg"
        assert fall["blackbox_entry"] == "1000_fall"
        ppe = store.incident("mechdog-02_2000_ppe")
        assert ppe["zone_id"] == "B" and ppe["snapshot_path"] is None
        store.review("mechdog-02_1000_fall", reviewed=True, resolution="ok", at_ms=9)
    finally:
        store.close()

    assert run(blackbox, db) == 0
    out = capsys.readouterr().out
    assert "inserted 0" in out and "existing 2" in out and "skipped 3" in out
    store = HistoryStore(db)
    try:
        assert store.incident("mechdog-02_1000_fall")["reviewed"] is True  # 검토 기록을 덮지 않는다
    finally:
        store.close()


def test_missing_blackbox_dir_exits_2(tmp_path):
    assert run(tmp_path / "none", tmp_path / "h.sqlite3") == 2


def test_empty_history_db_without_flag_exits_2(blackbox, monkeypatch, capsys):
    monkeypatch.setattr(history_import, "load_base_config", lambda: {"logging": {"history_db": ""}})
    assert history_import.main(["--device", "mechdog-02", "--blackbox", str(blackbox)]) == 2
    assert "history_db" in capsys.readouterr().err


def test_device_is_required(blackbox):
    with pytest.raises(SystemExit) as exc:
        history_import.main(["--blackbox", str(blackbox)])
    assert exc.value.code == 2


def test_incident_lookup_failure_counts_the_folder_as_failed_and_continues(
    blackbox, tmp_path, capsys, monkeypatch
):
    def broken(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(HistoryStore, "incident", broken)

    assert run(blackbox, tmp_path / "h.sqlite3") == 1
    assert "failed 2" in capsys.readouterr().out


def test_corrupt_existing_row_counts_as_failed_instead_of_crashing(blackbox, tmp_path, capsys):
    db = tmp_path / "h.sqlite3"
    assert run(blackbox, db) == 0
    capsys.readouterr()
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "UPDATE incidents SET detail = '{not json' WHERE incident_id = ?",
            ("mechdog-02_1000_fall",),
        )
    conn.close()

    assert run(blackbox, db) == 1
    out = capsys.readouterr().out
    assert "existing 1" in out and "failed 1" in out
