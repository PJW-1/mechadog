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
