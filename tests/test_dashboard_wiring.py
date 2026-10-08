"""관제 배선 단독 검증 — `host.dashboard.wiring` (Runtime 분할 7단계).

명령이 HTTP 에서 틱 전문까지 가는 시나리오는 `test_command_api.py`·`test_runtime.py` 에 있다.
여기서는 런타임 없이도 닿는 연결 규칙만 본다 — 길 찾기 유무, 카메라 공급자, 사건 전문 모양.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from host.dashboard.state import DashboardState
from host.dashboard.wiring import _has_navigator, _latest_jpeg, _map_view, _publish_event


def test_navigator_features_need_both_a_navigator_and_nav_status() -> None:
    """지도·항법 상태는 LiDAR 길 찾기가 붙은 런타임에만 연결한다."""
    bare = SimpleNamespace(_navigator=None, nav_status=lambda: {})
    assert not _has_navigator(bare)
    assert _map_view(bare) is None
    assert not _has_navigator(SimpleNamespace(_navigator=object()))
    assert _has_navigator(SimpleNamespace(_navigator=object(), nav_status=lambda: {}))


def test_latest_jpeg_reads_the_newest_frame_on_every_call() -> None:
    frames: list[Any] = [None, SimpleNamespace(jpeg=b"\xff\xd8one")]
    grab = _latest_jpeg(SimpleNamespace(latest=lambda: frames[0]))  # type: ignore[arg-type]
    assert grab() is None
    frames[0] = frames[1]
    assert grab() == b"\xff\xd8one"


def test_published_event_names_the_entry_and_never_ships_bytes(tmp_path: Path) -> None:
    """사건 전문에는 기록 디렉터리 이름과 그림 파일 이름만 싣는다 — 절대 경로·JPEG 없음."""
    board = DashboardState("mechdog-01", stale_after_ms=1000)
    entry_dir = tmp_path / "20261009-000000-person_found"
    entry = SimpleNamespace(
        event_type="person_found",
        ts_ms=1,
        state="ALERT",
        escalation="L1",
        mode="guard",
        tracks=[],
        detections=[],
        telemetry={"available": False},
        meta_path=entry_dir / "meta.json",
        jpeg_path=entry_dir / "frame.jpg",
        judgement={"zone": "A"},
        event_id="e1",
        session_id="s1",
        frame_id=7,
        config_sha256="abc",
        models=None,
        latency={"inference_ms": 1.0},
    )
    _publish_event(board)(entry)  # type: ignore[arg-type]
    (event,), _dropped = board.events_since(0)
    assert event["entry"] == entry_dir.name
    assert event["snapshot"] == "frame.jpg"
    assert str(tmp_path) not in repr(event)
    assert event["frame_id"] == 7 and event["judgement"] == {"zone": "A"}
