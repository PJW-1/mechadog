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


# ── 사건 스냅샷 되찾기 (WBS 4.6.4) ────────────────────────────


def test_snapshot_bytes_reads_the_recorded_jpeg(tmp_path, monkeypatch):
    """사건 전문에는 디렉터리 이름만 실으므로 그림은 이름으로 되찾는다 (4.4.3 · 4.6.4)."""
    import host.common.blackbox as module

    monkeypatch.setattr(module, "repo_path", lambda _p: tmp_path / "blackbox")
    box = module.EventBlackbox({"logging": {"blackbox_dir": "blackbox"}})
    entry = box.record(
        event_type="person_found",
        now_ms=1,
        state="PATROL",
        escalation="L1",
        tracks=[],
        detections=[],
        telemetry={},
        jpeg=b"\xff\xd8fake\xff\xd9",
    )
    name = entry.meta_path.parent.name
    assert box.snapshot_bytes(name) == b"\xff\xd8fake\xff\xd9"


@pytest.mark.parametrize(
    "entry",
    [
        "../secrets",
        "a/../../etc",
        "sub/dir",
        r"sub\dir",
        "",
        ".hidden",
        ".",
        "..",
    ],
)
def test_snapshot_bytes_refuses_names_that_leave_the_folder(entry, tmp_path, monkeypatch):
    """⚠️ **이름은 브라우저에서 온다.** 경로로 쓰기 전에 잘라야 한다.

    검증을 서버가 아니라 여기 두는 이유 — 저장 구조를 아는 것이 이 클래스뿐이고,
    양쪽에 규칙을 두면 한쪽만 고쳐지는 순간 폴더 밖 파일이 열린다.
    """
    import host.common.blackbox as module

    monkeypatch.setattr(module, "repo_path", lambda _p: tmp_path / "blackbox")
    box = module.EventBlackbox({"logging": {"blackbox_dir": "blackbox"}})
    (tmp_path / "secrets").mkdir(parents=True, exist_ok=True)
    (tmp_path / "secrets" / "snapshot.jpg").write_bytes(b"nope")
    assert box.snapshot_bytes(entry) is None


def test_snapshot_bytes_returns_none_for_an_event_without_a_picture(tmp_path, monkeypatch):
    """그림 없는 사건이 있다 — 없는 것을 빈 바이트로 만들지 않는다."""
    import host.common.blackbox as module

    monkeypatch.setattr(module, "repo_path", lambda _p: tmp_path / "blackbox")
    box = module.EventBlackbox({"logging": {"blackbox_dir": "blackbox"}})
    entry = box.record(
        event_type="person_found",
        now_ms=2,
        state="PATROL",
        escalation="L1",
        tracks=[],
        detections=[],
        telemetry={},
        jpeg=None,
    )
    assert box.snapshot_bytes(entry.meta_path.parent.name) is None


# ── 그릴 수 없는 판단 근거 (WBS 4.8.3 · 4.8.0) ────────────────────


def test_judgement_survives_the_round_trip(blackbox: EventBlackbox) -> None:
    """⚠️ **사진에 그릴 수 없는 것들이다.** 쓰러짐은 숫자고 VLM 판독은 문장이라,
    디스크를 거쳐 되돌아오지 않으면 *"왜 그렇게 판정했나"* 가 사라진다."""
    reason = {"fallen": True, "aspect": 3.25, "still_ms": 3000}
    entry = blackbox.record("person_fallen", now_ms=1000, judgement=reason)
    reason["fallen"] = False  # 넘긴 사전을 고쳐도 기록은 흔들리지 않는다

    assert entry.judgement == {"fallen": True, "aspect": 3.25, "still_ms": 3000}
    assert blackbox.feed()[0].judgement == entry.judgement


def test_records_written_before_judgement_existed_still_read(blackbox: EventBlackbox) -> None:
    """⚠️ 필수로 요구하면 `4.8.3` 이전에 쌓인 기록이 통째로 사라진다."""
    entry = blackbox.record("person_found", now_ms=1000)
    metadata = json.loads(entry.meta_path.read_text(encoding="utf-8"))
    del metadata["judgement"]
    entry.meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert blackbox.feed()[0].judgement == {}
