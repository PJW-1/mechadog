"""사건 이력 DB 검증 (WBS 4.6.6 · ADR-46).

DB 는 블랙박스의 **색인**이다 — 원본은 블랙박스 폴더에 있고, 쓰기 실패는 제어를 멈추지 않는다.
"""

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from host.common.blackbox import EventBlackbox
from host.common.history import (
    SEEN_WRITE_MS,
    HistoryStore,
    IncidentRow,
    incident_from_entry,
    incident_from_feed,
    incident_from_meta,
    open_history,
    zones_from_config,
)
from host.vision.detector import Detection
from host.vision.tracker import Track

ROBOT = "mechdog-02"


@pytest.fixture
def store(tmp_path: Path):
    opened = HistoryStore(tmp_path / "history" / "mechdog.sqlite3")
    opened.upsert_robot(ROBOT, serial_no="mechdog-3c8a1f1d80cc")
    yield opened
    opened.close()


def _incident(incident_id: str, at: int, **extra) -> IncidentRow:
    return IncidentRow(
        incident_id=incident_id, robot_id=ROBOT, occurred_at=at, event_type="person_found", **extra
    )


def test_schema_has_the_four_tables_and_a_version(store: HistoryStore) -> None:
    with sqlite3.connect(store.path) as raw:
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = raw.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        journal = raw.execute("PRAGMA journal_mode").fetchone()
    assert {"robots", "zones", "mission_runs", "incidents", "schema_meta"} <= tables
    assert version == ("1",)
    assert journal == ("wal",)


def test_reopening_keeps_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "h.sqlite3"
    first = HistoryStore(path)
    first.upsert_robot(ROBOT, serial_no="mac")
    first.record_incident(_incident("1000_person_found", 1000))
    first.close()

    again = HistoryStore(path)
    rows, total = again.incidents()
    again.close()
    assert total == 1
    assert rows[0]["incident_id"] == "1000_person_found"


def test_a_different_schema_version_is_refused(tmp_path: Path) -> None:
    """⚠️ 모르는 판의 DB 를 고쳐 쓰지 않는다 — 열기를 거부하고 런타임은 DB 없이 돈다."""
    path = tmp_path / "h.sqlite3"
    HistoryStore(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE schema_meta SET value='99' WHERE key='version'")
    with pytest.raises(sqlite3.DatabaseError, match="schema"):
        HistoryStore(path)


def test_robot_upsert_refreshes_the_serial_and_keeps_one_row(store: HistoryStore) -> None:
    store.upsert_robot(ROBOT, serial_no="mechdog-new")

    robots = store.robots()
    assert len(robots) == 1
    assert robots[0]["serial_no"] == "mechdog-new"
    assert robots[0]["display_name"] == ROBOT
    assert robots[0]["active"] is True


def test_zones_come_from_ids_hazard_ids_and_policies() -> None:
    config = {
        "zones": {
            "ids": ["A", "B", "C", "D"],
            "hazard_ids": ["C"],
            "policies": {
                "A": {"name": "출입구", "helmet": True, "vest": False, "note": "정문"},
                "B": {"helmet": False, "vest": False},
            },
        }
    }

    rows = {row.zone_id: row for row in zones_from_config(config)}

    assert list(rows) == ["A", "B", "C", "D"]
    assert (rows["A"].zone_name, rows["A"].ppe_policy, rows["A"].description) == (
        "출입구",
        "helmet",
        "정문",
    )
    assert rows["B"].ppe_policy == "none"
    # 정책이 없는 구역은 안전모·조끼 모두 필수다 (config.yaml `zones.policies` 주석).
    assert (rows["C"].zone_name, rows["C"].ppe_policy, rows["C"].risk_level) == (
        "C",
        "helmet+vest",
        "hazard",
    )
    assert rows["D"].risk_level == "normal"


def test_sync_zones_replaces_attributes_and_retires_missing_zones(store: HistoryStore) -> None:
    store.sync_zones(zones_from_config({"zones": {"ids": ["A", "B"], "hazard_ids": ["B"]}}))
    store.sync_zones(zones_from_config({"zones": {"ids": ["A"], "hazard_ids": ["A"]}}))

    zones = {zone["zone_id"]: zone for zone in store.zones()}
    assert zones["A"]["risk_level"] == "hazard"
    # 지난 사건이 가리키므로 지우지 않고 쉬게 한다.
    assert zones["B"]["active"] is False


def test_a_run_collects_zones_in_first_arrival_order_and_counts_incidents(
    store: HistoryStore,
) -> None:
    mission = store.open_run(ROBOT, mode="factory", started_at=1000)
    assert mission == f"{ROBOT}-1000"
    for zone in ("A", "A", "B", "A", "C"):
        store.visit_zone(mission, zone)
    store.record_incident(_incident("1500_person_found", 1500, mission_id=mission))
    store.close_run(mission, ended_at=9000, result="failsafe", stop_reason="ESTOP")

    runs, total = store.runs()
    assert total == 1
    run = runs[0]
    assert run["zones_visited"] == ["A", "B", "C"]
    assert run["incident_count"] == 1
    assert (run["ended_at"], run["result"], run["stop_reason"]) == (9000, "failsafe", "ESTOP")


def test_runs_left_open_by_a_crash_are_closed_as_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "h.sqlite3"
    crashed = HistoryStore(path)
    crashed.upsert_robot(ROBOT, serial_no=None)
    crashed.open_run(ROBOT, mode="guard", started_at=1000)
    crashed.close()

    restarted = HistoryStore(path)
    closed = restarted.close_open_runs(ROBOT, ended_at=5000)
    runs, _ = restarted.runs()
    restarted.close()
    assert closed == 1
    assert (runs[0]["ended_at"], runs[0]["result"]) == (5000, "interrupted")


def test_a_run_of_an_unregistered_robot_is_kept(tmp_path: Path) -> None:
    """⚠️ 기동 때 기체 등록이 실패했어도 그 세션의 순찰 판을 잃지 않는다 — 자리표 행을 만든다."""
    store = HistoryStore(tmp_path / "h.sqlite3")
    mission = store.open_run("mechdog-09", mode="guard", started_at=1000)
    robots = store.robots()
    store.close()
    assert mission == "mechdog-09-1000"
    assert [(r["robot_id"], r["display_name"]) for r in robots] == [("mechdog-09", "mechdog-09")]


def test_the_same_incident_is_stored_once(store: HistoryStore) -> None:
    """가져오기 도구를 두 번 돌려도, 같은 사건을 두 경로가 넣어도 한 건이다."""
    mission = store.open_run(ROBOT, mode="guard", started_at=1000)
    assert store.record_incident(_incident("1500_person_found", 1500, mission_id=mission))
    assert not store.record_incident(_incident("1500_person_found", 1500, mission_id=mission))

    _, total = store.incidents()
    runs, _ = store.runs()
    assert total == 1
    assert runs[0]["incident_count"] == 1


def test_an_unknown_zone_gets_a_placeholder_instead_of_losing_the_incident(
    store: HistoryStore,
) -> None:
    assert store.record_incident(_incident("1_x", 1, zone_id="Z"))

    zones = {zone["zone_id"]: zone for zone in store.zones()}
    assert zones["Z"]["zone_name"] == "Z"
    assert store.incidents(zone="Z")[1] == 1


def _blackbox(tmp_path: Path) -> EventBlackbox:
    return EventBlackbox({"logging": {"blackbox_dir": str(tmp_path / "blackbox")}})


def test_a_blackbox_entry_becomes_an_incident_that_points_back_at_its_folder(
    tmp_path: Path,
) -> None:
    entry = _blackbox(tmp_path).record(
        "hazard_notice",
        jpeg=b"\xff\xd8jpeg",
        tracks=(Track(track_id=3, box=(0.0, 0.0, 1.0, 1.0), score=0.7, last_seen_ms=1),),
        detections=(Detection("lighter", 0.91, (0.0, 0.0, 1.0, 1.0)),),
        judgement={"zone": "C", "items": ["lighter"], "source": "detector"},
        state="ZONE_INSPECT",
        escalation="L0",
        mode="factory",
        now_ms=1789378867729,
    )

    row = incident_from_entry(entry, robot_id=ROBOT, mission_id="m", zone_id="A")

    name = entry.meta_path.parent.name
    assert row.incident_id == f"{ROBOT}_{name}"
    assert row.blackbox_entry == name
    assert row.snapshot_path == f"{name}/snapshot.jpg"
    # 판단 근거의 구역이 지금 서 있는 구역보다 앞선다 — 판독은 그 구역에서 건 것이다.
    assert row.zone_id == "C"
    assert row.confidence == pytest.approx(0.91)
    assert (row.occurred_at, row.event_type, row.state, row.escalation_level, row.mode) == (
        1789378867729,
        "hazard_notice",
        "ZONE_INSPECT",
        "L0",
        "factory",
    )
    assert row.detail == {"zone": "C", "items": ["lighter"], "source": "detector"}


def test_two_robots_with_the_same_folder_name_keep_both_incidents(tmp_path: Path) -> None:
    """⚠️ 여러 대는 블랙박스 폴더는 나누고 DB 는 하나다 — 같은 밀리초에 같은 사건을 남기면
    폴더 이름이 같다. 사건 ID 에 기체를 붙여 둘 다 남긴다."""
    store = HistoryStore(tmp_path / "h.sqlite3")
    meta = {"ts_ms": 1000, "event": "ppe_violation"}
    kept = [
        store.record_incident(incident_from_meta("1000_ppe_violation", meta, robot_id=robot))
        for robot in ("mechdog-01", "mechdog-02")
    ]
    rows, total = store.incidents()
    store.close()
    assert kept == [True, True] and total == 2
    assert {(r["robot_id"], r["blackbox_entry"]) for r in rows} == {
        ("mechdog-01", "1000_ppe_violation"),
        ("mechdog-02", "1000_ppe_violation"),
    }


def test_an_entry_without_a_picture_or_scores_keeps_nulls(tmp_path: Path) -> None:
    entry = _blackbox(tmp_path).record("PPE_SETTLED", now_ms=5)

    row = incident_from_entry(entry, robot_id=ROBOT, mission_id=None, zone_id="D")

    assert row.snapshot_path is None
    assert row.blackbox_entry == entry.meta_path.parent.name
    assert row.confidence is None
    assert row.zone_id == "D"


def test_a_feed_only_event_keeps_its_extra_fields_as_detail() -> None:
    payload = {
        "event": "escalation_changed",
        "ts_ms": 7000,
        "state": "ALERT",
        "escalation": "L3",
        "mode": "factory",
        "warning": "보호구 미착용",
        "reason": "PPE_VIOLATION",
    }

    row = incident_from_feed(payload, robot_id=ROBOT, mission_id="m", zone_id=None)

    assert row.incident_id.startswith("7000_escalation_changed_")
    assert (row.event_type, row.state, row.escalation_level, row.mode) == (
        "escalation_changed",
        "ALERT",
        "L3",
        "factory",
    )
    assert row.detail == {"warning": "보호구 미착용", "reason": "PPE_VIOLATION"}
    assert row.snapshot_path is None
    assert row.blackbox_entry is None


def test_two_identical_feed_events_in_the_same_millisecond_are_both_kept() -> None:
    payload = {"event": "auth_failed", "ts_ms": 1, "state": "ALERT", "escalation": "L3"}

    first = incident_from_feed(payload, robot_id=ROBOT, mission_id=None, zone_id=None)
    second = incident_from_feed(payload, robot_id=ROBOT, mission_id=None, zone_id=None)

    assert first.incident_id != second.incident_id


def test_search_filters_combine_and_list_newest_first(store: HistoryStore) -> None:
    other = "mechdog-01"
    store.upsert_robot(other, serial_no=None)
    mission = store.open_run(ROBOT, mode="factory", started_at=0)
    store.record_incident(_incident("100_a", 100, zone_id="A", escalation_level="L0"))
    store.record_incident(
        IncidentRow(
            incident_id="200_b",
            robot_id=ROBOT,
            occurred_at=200,
            event_type="PPE_VIOLATION",
            zone_id="D",
            escalation_level="L3",
            mission_id=mission,
        )
    )
    store.record_incident(_incident("300_c", 300, zone_id="D", escalation_level="L0"))
    store.record_incident(
        IncidentRow(incident_id="400_d", robot_id=other, occurred_at=400, event_type="person_found")
    )

    rows, total = store.incidents()
    assert total == 4
    assert [row["incident_id"] for row in rows] == ["400_d", "300_c", "200_b", "100_a"]
    assert [r["incident_id"] for r in store.incidents(since=200, until=300)[0]] == [
        "300_c",
        "200_b",
    ]
    assert [r["incident_id"] for r in store.incidents(zone="D", escalation="L3")[0]] == ["200_b"]
    assert [r["incident_id"] for r in store.incidents(event="PPE_VIOLATION")[0]] == ["200_b"]
    assert [r["incident_id"] for r in store.incidents(robot=other)[0]] == ["400_d"]
    assert [r["incident_id"] for r in store.incidents(mission=mission)[0]] == ["200_b"]
    page, total = store.incidents(limit=2, offset=1)
    assert total == 4
    assert [row["incident_id"] for row in page] == ["300_c", "200_b"]


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (501, 0), (10, -1), (True, 0)])
def test_paging_out_of_range_is_refused(store: HistoryStore, limit, offset) -> None:
    with pytest.raises(ValueError):
        store.incidents(limit=limit, offset=offset)
    with pytest.raises(ValueError):
        store.runs(limit=limit, offset=offset)


def test_review_is_saved_and_can_be_searched(store: HistoryStore) -> None:
    store.record_incident(_incident("100_a", 100, detail={"zone": "A"}))
    store.record_incident(_incident("200_b", 200))

    updated = store.review("100_a", reviewed=True, resolution="오경보 — 마네킹", at_ms=999)

    assert updated is not None
    assert (updated["reviewed"], updated["resolution"], updated["reviewed_at"]) == (
        True,
        "오경보 — 마네킹",
        999,
    )
    assert updated["detail"] == {"zone": "A"}
    assert [row["incident_id"] for row in store.incidents(reviewed=False)[0]] == ["200_b"]
    reopened = store.review("100_a", reviewed=False, resolution="", at_ms=1000)
    assert reopened is not None
    assert (reopened["reviewed"], reopened["reviewed_at"]) == (False, None)


def test_review_refuses_unknown_ids_and_overlong_notes(store: HistoryStore) -> None:
    store.record_incident(_incident("100_a", 100))

    assert store.review("nope", reviewed=True, resolution="", at_ms=1) is None
    with pytest.raises(ValueError):
        store.review("100_a", reviewed=True, resolution="x" * 2001, at_ms=1)


def test_last_seen_is_written_at_most_every_few_seconds(store: HistoryStore) -> None:
    """텔레메트리는 10Hz 다 — 매번 쓰면 기록이 제어 주기를 갉아먹는다."""

    def seen() -> tuple[int | None, str | None]:
        robot = store.robots()[0]
        return robot["last_seen_at"], robot["status"]

    assert store.note_seen(ROBOT, at_ms=10_000, status="PATROL")
    assert not store.note_seen(ROBOT, at_ms=10_000 + SEEN_WRITE_MS - 1, status="PATROL")
    assert seen() == (10_000, "PATROL")
    # 상태가 바뀌면 기다리지 않는다.
    assert store.note_seen(ROBOT, at_ms=10_500, status="ALERT")
    assert seen() == (10_500, "ALERT")
    assert store.note_seen(ROBOT, at_ms=10_500 + SEEN_WRITE_MS, status="ALERT")


def test_write_failures_are_reported_not_raised(store: HistoryStore) -> None:
    """⚠️ 기록 실패가 10Hz 제어를 죽이면 안 된다 — 블랙박스와 같은 규칙이다."""
    store.close()

    assert not store.record_incident(_incident("1_a", 1))
    assert store.open_run(ROBOT, mode="guard", started_at=1) is None
    assert not store.visit_zone("m", "A")
    assert not store.close_run("m", ended_at=2, result="stopped", stop_reason=None)
    assert store.close_open_runs(ROBOT, ended_at=2) == 0
    assert not store.note_seen(ROBOT, at_ms=1, status="IDLE")
    assert not store.upsert_robot(ROBOT, serial_no=None)
    assert not store.sync_zones([])


def test_open_history_follows_the_config(tmp_path: Path) -> None:
    opened = open_history({"logging": {"history_db": str(tmp_path / "a" / "h.sqlite3")}})
    assert opened is not None
    opened.close()
    # 비우면 끈다 — 이 DB 는 색인이라 없어도 운용은 된다.
    assert open_history({"logging": {"history_db": ""}}) is None
    assert open_history({"logging": {}}) is None


def test_open_history_returns_none_when_the_file_cannot_be_opened(tmp_path: Path) -> None:
    blocked = tmp_path / "is-a-directory"
    blocked.mkdir()

    assert open_history({"logging": {"history_db": str(blocked)}}) is None


def _failures(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == "history_write_failed"]


def test_a_broken_column_is_reported_not_raised(store: HistoryStore, caplog) -> None:
    """⚠️ sqlite3.Error 가 아닌 예외도 10Hz 루프까지 올라가면 안 된다."""
    mission = store.open_run(ROBOT, mode="factory", started_at=1000)
    with sqlite3.connect(store.path) as raw:
        raw.execute("UPDATE mission_runs SET zones_visited = 'not json'")

    with caplog.at_level(logging.ERROR, logger="mechadog.history"):
        assert not store.visit_zone(mission, "A")

    assert len(_failures(caplog)) == 1


def test_a_failing_last_seen_write_is_not_retried_every_telemetry(
    store: HistoryStore, caplog
) -> None:
    store.close()

    with caplog.at_level(logging.ERROR, logger="mechadog.history"):
        assert not store.note_seen(ROBOT, at_ms=10_000, status="PATROL")
        assert not store.note_seen(ROBOT, at_ms=10_000 + SEEN_WRITE_MS - 1, status="PATROL")
        assert len(_failures(caplog)) == 1
        # 상태가 바뀌거나 간격이 지나면 다시 시도한다.
        assert not store.note_seen(ROBOT, at_ms=10_500, status="ALERT")
        assert len(_failures(caplog)) == 2
        assert not store.note_seen(ROBOT, at_ms=10_500 + SEEN_WRITE_MS, status="ALERT")
        assert len(_failures(caplog)) == 3


def _hold_write_lock(path: Path) -> sqlite3.Connection:
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_a_held_write_lock_does_not_stall_the_writer(tmp_path: Path) -> None:
    """런타임 틱은 100ms, 로봇 명령 타임아웃은 600ms 다 — 잠금 대기가 길면 제어가 멈춘다."""
    opened = HistoryStore(tmp_path / "h.sqlite3", write_timeout_s=0.05)
    holder = _hold_write_lock(opened.path)
    try:
        started = time.monotonic()
        assert not opened.record_incident(_incident("1_a", 1))
        assert time.monotonic() - started < 0.5
    finally:
        holder.close()
        opened.close()


def test_open_history_waits_only_briefly_for_the_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "h.sqlite3"
    opened = open_history({"logging": {"history_db": str(path)}})
    assert opened is not None
    holder = _hold_write_lock(path)
    try:
        started = time.monotonic()
        assert not opened.record_incident(_incident("1_a", 1))
        assert time.monotonic() - started < 0.5
    finally:
        holder.close()
        opened.close()
