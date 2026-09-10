"""이벤트 블랙박스 저장 계층 검증 (WBS 4.4.3 일부)."""

import json
from pathlib import Path

import pytest

from host.common.blackbox import EventBlackbox
from host.vision.detector import Detection
from host.vision.tracker import Track


@pytest.fixture
def blackbox(tmp_path: Path) -> EventBlackbox:
    return EventBlackbox({"logging": {"blackbox_dir": str(tmp_path / "blackbox")}})


def _track(track_id: int, x: float) -> Track:
    return Track(
        track_id=track_id,
        box=(x, 50.0, x + 100.0, 350.0),
        score=0.9,
        last_seen_ms=1000,
    )


def test_record_preserves_jpeg_and_complete_context(blackbox: EventBlackbox) -> None:
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    telemetry = {"device_id": "dog-01", "seq": 42, "flags": {"obstacle": False}}
    entry = blackbox.record(
        "person_found",
        jpeg=jpeg,
        tracks=(_track(1, 100), _track(2, 300)),
        detections=(Detection("person", 0.95, (100.0, 50.0, 200.0, 350.0)),),
        telemetry=telemetry,
        state="PATROL",
        escalation="L0",
        now_ms=1000,
    )
    telemetry["flags"]["obstacle"] = True

    assert entry.jpeg_path is not None
    assert entry.jpeg_path.read_bytes() == jpeg
    metadata = json.loads(entry.meta_path.read_text(encoding="utf-8"))
    assert metadata["event"] == "person_found"
    assert metadata["state"] == "PATROL"
    assert metadata["escalation"] == "L0"
    assert [item["track_id"] for item in metadata["tracks"]] == [1, 2]
    assert metadata["detections"][0]["box"] == [100.0, 50.0, 200.0, 350.0]
    assert metadata["telemetry"]["flags"]["obstacle"] is False


def test_same_timestamp_and_event_never_overwrite(blackbox: EventBlackbox) -> None:
    first = blackbox.record("person_found", now_ms=1000)
    second = blackbox.record("person_found", now_ms=1000)

    assert first.meta_path != second.meta_path
    assert len(blackbox.feed()) == 2


def test_event_name_cannot_escape_storage_root(blackbox: EventBlackbox) -> None:
    entry = blackbox.record("../../person found", now_ms=1000)

    assert entry.meta_path.resolve().is_relative_to(blackbox._dir.resolve())  # noqa: SLF001
    assert ".." not in entry.meta_path.parent.name
    assert json.loads(entry.meta_path.read_text(encoding="utf-8"))["event"] == "../../person found"


def test_feed_sorts_filters_and_ignores_partial_records(blackbox: EventBlackbox) -> None:
    blackbox.record("later", now_ms=2000)
    blackbox.record("earlier", now_ms=1000)
    partial = blackbox.latest.meta_path.parents[1] / "3000_partial"
    partial.mkdir()
    (partial / "snapshot.jpg").write_bytes(b"partial")

    assert [entry.ts_ms for entry in blackbox.feed()] == [1000, 2000]
    assert [entry.ts_ms for entry in blackbox.feed(since_ms=1000)] == [2000]
    assert blackbox.latest is not None and blackbox.latest.ts_ms == 2000


def test_feed_ignores_malformed_metadata(blackbox: EventBlackbox) -> None:
    malformed = blackbox.latest
    assert malformed is None
    directory = Path(blackbox._dir) / "1000_broken"  # noqa: SLF001 - 손상 파일 주입 시험
    directory.mkdir()
    (directory / "meta.json").write_text("{broken", encoding="utf-8")

    assert blackbox.feed() == []


@pytest.mark.parametrize(("event_type", "now_ms"), [("", 0), ("event", -1), ("event", True)])
def test_invalid_record_identity_is_rejected(
    blackbox: EventBlackbox, event_type: str, now_ms: int
) -> None:
    with pytest.raises(ValueError):
        blackbox.record(event_type, now_ms=now_ms)
