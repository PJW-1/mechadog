"""VLM 학습용 프레임 수집기 (WBS 4.8.7) — 폴더·간격·상한·설정 검증을 소켓 없이 닫는다."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from host.common.config import ConfigError, load_base_config, validate_base_config
from host.vision.frame_collector import FrameCollector, collector_from_config

JPEG = b"\xff\xd8test\xff\xd9"
T0 = int(datetime(2026, 10, 5, 12, 0, 0).timestamp() * 1000)
DAY = "20261005"


def _make(root: Path | None, **kwargs: int) -> FrameCollector:
    return FrameCollector(root, "mechdog-01", **kwargs)


def _lines(root: Path) -> list[dict]:
    path = root / DAY / "manifest.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _clear(c: FrameCollector, now: int, **kw: Any) -> bool:
    args: dict[str, Any] = {"state": "PATROL", "obstacle_active": False, "pending": False}
    args.update(kw)
    return c.note_clear(now, JPEG, **args)


def test_unset_root_writes_nothing(tmp_path: Path) -> None:
    c = _make(None)
    assert not c.enabled
    assert _clear(c, T0) is False
    assert c.note_blocked(T0, JPEG, (1.0, 2.0), "A", "PATROL") is False
    assert list(tmp_path.iterdir()) == []


def test_clear_respects_interval(tmp_path: Path) -> None:
    c = _make(tmp_path, clear_every_ms=5000)
    assert _clear(c, T0)
    assert not _clear(c, T0 + 4999)
    assert _clear(c, T0 + 5000)
    assert len(list((tmp_path / DAY / "clear").glob("*.jpg"))) == 2


def test_blocked_every_confirmation_and_holdoff(tmp_path: Path) -> None:
    c = _make(tmp_path, clear_every_ms=5000, clear_holdoff_ms=10000)
    assert c.note_blocked(T0, JPEG, (1.234, 2.0), "B", "PATROL")
    assert c.note_blocked(T0 + 100, JPEG, (1.5, 2.0), "B", "PATROL")
    assert not _clear(c, T0 + 10099)
    assert _clear(c, T0 + 10100)
    assert len(list((tmp_path / DAY / "blocked").glob("*.jpg"))) == 2


def test_clear_skipped_while_obstacle_active_or_pending(tmp_path: Path) -> None:
    c = _make(tmp_path)
    assert not _clear(c, T0, obstacle_active=True)
    assert not _clear(c, T0 + 1, pending=True)
    assert _clear(c, T0 + 2)  # 거절된 시도가 간격을 소모하지 않는다


def test_ignored_outside_patrol(tmp_path: Path) -> None:
    c = _make(tmp_path)
    assert not _clear(c, T0, state="TRACK")
    assert not c.note_blocked(T0, JPEG, (0.0, 0.0), "A", "ALERT")
    assert not (tmp_path / DAY).exists()


def test_max_files_caps_and_warns_once(tmp_path: Path) -> None:
    c = _make(tmp_path, clear_every_ms=1, max_files=2)
    assert _clear(c, T0) and _clear(c, T0 + 10)
    assert not _clear(c, T0 + 20)
    assert c.full
    assert not c.note_blocked(T0 + 30, JPEG, (0.0, 0.0), "A", "PATROL")
    assert len(list(tmp_path.rglob("*.jpg"))) == 2
    assert len(_lines(tmp_path)) == 2


def test_cap_warning_logged_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from host.vision import frame_collector

    warnings: list[str] = []
    monkeypatch.setattr(frame_collector.LOG, "warning", lambda event, **_kw: warnings.append(event))
    c = _make(tmp_path, clear_every_ms=1, max_files=1)
    _clear(c, T0)
    for i in range(1, 5):
        _clear(c, T0 + 10 * i)
    assert warnings == ["frame_collect_full"]


def test_existing_files_count_toward_cap(tmp_path: Path) -> None:
    first = _make(tmp_path, max_files=1)
    assert _clear(first, T0)
    second = _make(tmp_path, max_files=1)
    assert not _clear(second, T0 + 10000)


def test_manifest_line_format(tmp_path: Path) -> None:
    c = _make(tmp_path)
    c.note_blocked(T0, JPEG, (1.234, -2.0), "B", "PATROL")
    _clear(c, T0 + 20000)
    blocked, clear = _lines(tmp_path)
    assert blocked == {
        "file": blocked["file"],
        "at_ms": T0,
        "device_id": "mechdog-01",
        "label": "blocked",
        "source": "lidar",
        "x": 1.23,
        "y": -2.0,
        "target": "B",
        "state": "PATROL",
    }
    assert blocked["file"].startswith("blocked/") and blocked["file"].endswith(".jpg")
    assert (tmp_path / DAY / blocked["file"]).read_bytes() == JPEG
    assert clear["label"] == "clear" and "x" not in clear and clear["state"] == "PATROL"


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = _make(blocker)  # 폴더를 만들 수 없는 경로
    assert _clear(c, T0) is False


def test_config_default_off_and_outside_path(tmp_path: Path) -> None:
    cfg = deepcopy(load_base_config())
    assert cfg["vision"]["collect"]["root"] is None
    assert not collector_from_config(cfg, "mechdog-01").enabled
    cfg["vision"]["collect"]["root"] = str(tmp_path)
    validate_base_config(cfg)
    assert collector_from_config(cfg, "mechdog-01").enabled


@pytest.mark.parametrize("root", ["data/collect", str(Path(__file__).resolve().parent)])
def test_config_rejects_path_inside_repo(root: str) -> None:
    cfg = deepcopy(load_base_config())
    cfg["vision"]["collect"]["root"] = root
    with pytest.raises(ConfigError, match="저장소 밖"):
        validate_base_config(cfg)


def test_config_rejects_bad_numbers() -> None:
    cfg = deepcopy(load_base_config())
    cfg["vision"]["collect"]["max_files"] = 0
    with pytest.raises(ConfigError, match="max_files"):
        validate_base_config(cfg)


def test_stale_frame_is_not_saved_as_blocked_but_holdoff_still_applies(tmp_path: Path) -> None:
    c = _make(tmp_path, clear_holdoff_ms=10000, max_frame_age_ms=1000)
    # 비전이 끊겨 마지막 결과가 낡았다. 확정 시점과 다른 장면이라 blocked 라벨을 붙이지 않는다.
    assert not c.note_blocked(T0, JPEG, (1.0, 2.0), "B", "PATROL", frame_ms=T0 - 1001)
    assert not (tmp_path / DAY / "blocked").exists()
    assert not _clear(c, T0 + 9999, frame_ms=T0 + 9999), "막힘 사건은 있었으므로 보류 시간은 건다"
    assert _clear(c, T0 + 10000, frame_ms=T0 + 10000)


def test_frame_exactly_at_age_limit_is_saved(tmp_path: Path) -> None:
    c = _make(tmp_path, max_frame_age_ms=1000)
    assert c.note_blocked(T0, JPEG, (1.0, 2.0), "B", "PATROL", frame_ms=T0 - 1000)


def test_blocked_without_frame_still_applies_holdoff(tmp_path: Path) -> None:
    c = _make(tmp_path, clear_holdoff_ms=10000)
    assert not c.note_blocked(T0, None, (1.0, 2.0), "B", "PATROL")
    assert not _clear(c, T0 + 5000)
    assert _clear(c, T0 + 10000)


def test_failed_blocked_save_still_applies_holdoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _make(tmp_path, clear_holdoff_ms=10000)
    real = Path.write_bytes
    calls = {"n": 0}

    def flaky(self: Path, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("디스크 일시 오류")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    assert not c.note_blocked(T0, JPEG, (1.0, 2.0), "B", "PATROL")
    assert not _clear(c, T0 + 5000)
    assert _clear(c, T0 + 10000)


def test_stale_clear_frame_is_skipped(tmp_path: Path) -> None:
    c = _make(tmp_path, max_frame_age_ms=1000)
    assert not _clear(c, T0, frame_ms=T0 - 1001)
    assert _clear(c, T0 + 1, frame_ms=T0)  # 거절된 낡은 프레임은 간격을 소모하지 않는다


def test_manifest_records_frame_time_and_seq(tmp_path: Path) -> None:
    c = _make(tmp_path)
    c.note_blocked(T0, JPEG, (1.0, 2.0), "B", "PATROL", frame_ms=T0 - 40, frame_seq=17)
    _clear(c, T0 + 20000, frame_ms=T0 + 19990, frame_seq=99)
    blocked, clear = _lines(tmp_path)
    assert (blocked["frame_ms"], blocked["frame_seq"]) == (T0 - 40, 17)
    assert (clear["frame_ms"], clear["frame_seq"]) == (T0 + 19990, 99)


def test_startup_count_ignores_unrelated_jpgs(tmp_path: Path) -> None:
    (tmp_path / "unrelated.jpg").write_bytes(JPEG)
    (tmp_path / "deep" / "a" / "b").mkdir(parents=True)
    (tmp_path / "deep" / "a" / "b" / "x.jpg").write_bytes(JPEG)
    c = _make(tmp_path, max_files=1)
    assert _clear(c, T0), "수집기 배치(<날짜>/<label>/*.jpg) 밖의 사진은 상한에 세지 않는다"


def test_startup_count_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("읽을 수 없는 폴더")

    monkeypatch.setattr(Path, "glob", boom)
    monkeypatch.setattr(Path, "rglob", boom)
    c = _make(tmp_path)
    assert c.enabled and not c.full


def test_relative_root_is_anchored_at_repo_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from host.common.config import ROOT

    monkeypatch.chdir(tmp_path)
    c = collector_from_config({"vision": {"collect": {"root": "../outside-collect"}}}, "d")
    assert c.root == ROOT / "../outside-collect"


def test_collector_from_config_passes_limits(tmp_path: Path) -> None:
    spec = {
        "root": str(tmp_path),
        "clear_every_ms": 1,
        "clear_holdoff_ms": 2,
        "max_files": 3,
        "max_frame_age_ms": 4,
    }
    c = collector_from_config({"vision": {"collect": spec}}, "d")
    assert (c.clear_every_ms, c.clear_holdoff_ms, c.max_files, c.max_frame_age_ms) == (1, 2, 3, 4)


@pytest.mark.parametrize(
    ("collect", "match"),
    [(["root"], "매핑"), ({"root": 123}, "문자열"), ({"root": "  "}, "문자열")],
)
def test_config_rejects_malformed_collect(collect: Any, match: str) -> None:
    cfg = deepcopy(load_base_config())
    cfg["vision"]["collect"] = collect
    with pytest.raises(ConfigError, match=match):
        validate_base_config(cfg)


def test_config_rejects_bad_frame_age() -> None:
    cfg = deepcopy(load_base_config())
    cfg["vision"]["collect"]["max_frame_age_ms"] = 0
    with pytest.raises(ConfigError, match="max_frame_age_ms"):
        validate_base_config(cfg)
