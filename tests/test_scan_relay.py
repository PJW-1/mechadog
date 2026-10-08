"""LiDAR 스캔 중계 단독 검증 — `host.behavior.scan_relay.ScanRelay` (Runtime 분할 12단계).

실제 길 찾기·구역 점검과 맞물리는 시나리오는 `test_runtime_lidar.py` 가 `Runtime` 으로 본다.
여기서는 런타임 없이 위치 전파, 측위 기록, 새 장애물의 막힘 기록 스위치를 본다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from host.behavior.scan_relay import ScanRelay
from host.common.lidar_link import Scan

SCAN = Scan("lidar-01", "0" * 16, 2, 0, ((0.0, 3.0),))


class Navigator:
    def __init__(self) -> None:
        self.pose: tuple[float, float, float] = (1.0, 2.0, 0.5)
        self.pose_ms: int | None = None
        self.match_frac = 0.8
        self.pose_verified = True
        self.phase = SimpleNamespace(value="drive")
        self.target = "A"
        self.halt_reason = None
        self.scan_gate = SimpleNamespace(status="open")
        self.obstacles: list[tuple[float, float]] = []
        self.stale = False

    def observe_scan(self, _scan: Scan, now_ms: int) -> None:
        self.pose_ms = now_ms

    def pose_stale(self, _now_ms: int) -> bool:
        return self.stale

    def take_new_obstacles(self) -> list[tuple[float, float]]:
        taken, self.obstacles = self.obstacles, []
        return taken


class Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, kind: str, **fields: Any) -> None:
        self.records.append((kind, fields))


def _relay(
    navigator: Any, *, state: str = "PATROL", nav: dict[str, Any] | None = None
) -> tuple[ScanRelay, dict[str, list[Any]]]:
    seen: dict[str, list[Any]] = {"zone_ppe": [], "inspector": [], "pose": [], "blocked": []}
    relay = ScanRelay(
        {"nav": nav or {}},
        device_id="mechdog-01",
        navigator=lambda: navigator,
        behavior=SimpleNamespace(state=state),  # type: ignore[arg-type]
        zone_ppe=SimpleNamespace(  # type: ignore[arg-type]
            note_pose=lambda pose, now_ms: seen["zone_ppe"].append((pose, now_ms)),
            current_zone=lambda _now_ms: "A",
        ),
        zone_inspector=SimpleNamespace(  # type: ignore[arg-type]
            note_pose=lambda pose, now_ms: seen["inspector"].append((pose, now_ms))
        ),
        path_cause=SimpleNamespace(  # type: ignore[arg-type]
            blocked=lambda judgement, _result, _now_ms: seen["blocked"].append(judgement)
        ),
        latest=lambda: None,
        pose_out=SimpleNamespace(  # type: ignore[arg-type]
            send=lambda pose, **fields: seen["pose"].append((pose, fields))
        ),
        recorder=Recorder(),  # type: ignore[arg-type]
    )
    return relay, seen


def test_note_pose_fans_out_to_zones_and_dashboard() -> None:
    relay, seen = _relay(Navigator())
    relay.note_pose((1.0, 2.0, 0.5), 100)
    assert seen["zone_ppe"] == [((1.0, 2.0, 0.5), 100)]
    assert seen["inspector"] == [((1.0, 2.0, 0.5), 100)]
    assert seen["pose"] == [
        ((1.0, 2.0, 0.5), {"moving": True, "score_frac": 0.8, "zone": "A", "verified": True})
    ]


def test_observe_without_navigator_or_scans_does_nothing() -> None:
    relay, seen = _relay(None)
    relay.attach(lambda: SCAN)
    relay.observe(100)
    relay, seen = _relay(Navigator())
    relay.observe(100)  # 스캔 공급자가 아직 붙지 않았다
    assert seen["pose"] == []
    assert relay._recorder.records == []  # type: ignore[union-attr]


def test_observe_updates_pose_and_records_localization_and_phase_once() -> None:
    navigator = Navigator()
    relay, seen = _relay(navigator)
    relay.attach(lambda: SCAN)
    relay.observe(100)
    relay.observe(200)
    assert [pose for pose, _ in seen["pose"]] == [(1.0, 2.0, 0.5)] * 2
    kinds = [kind for kind, _ in relay._recorder.records]  # type: ignore[union-attr]
    assert kinds == ["localization", "navigator_phase", "localization"]
    localization = relay._recorder.records[0][1]  # type: ignore[union-attr]
    assert localization["scan_seq"] == 2
    assert localization["pose"] == [1.0, 2.0, 0.5]
    assert localization["phase"] == "drive"


def test_stale_pose_is_sent_as_lost() -> None:
    navigator = Navigator()
    navigator.pose_ms = 50
    navigator.stale = True
    relay, seen = _relay(navigator)
    navigator.observe_scan = lambda _scan, _now_ms: None  # type: ignore[method-assign]
    relay.attach(lambda: SCAN)
    relay.observe(100)
    assert seen["pose"] == [
        (
            (1.0, 2.0, 0.5),
            {"moving": False, "lost": True, "score_frac": 0.8, "zone": "A", "verified": False},
        )
    ]


@pytest.mark.parametrize(
    ("publish", "state", "expected"),
    [
        (False, "PATROL", []),
        (True, "PATROL", [{"x": 1.23, "y": 4.57, "target": "A", "source": "lidar"}]),
        (True, "TRACK", []),
    ],
)
def test_new_obstacles_become_path_blocks_only_when_published_on_patrol(
    publish: bool, state: str, expected: list[dict[str, Any]]
) -> None:
    navigator = Navigator()
    navigator.obstacles = [(1.234, 4.567)]
    relay, seen = _relay(navigator, state=state, nav={"publish_new_obstacles": publish})
    relay.attach(lambda: None)
    relay.observe(100)
    assert seen["blocked"] == expected
    assert navigator.obstacles == [], "새 장애물은 한 번만 가져간다"


def test_collector_is_off_without_a_root() -> None:
    relay, _ = _relay(Navigator())
    assert not relay.collector.enabled
    relay.collect_blocked((0.0, 0.0), 100)
    relay.collect_clear(SimpleNamespace(jpeg=b""), 100)  # type: ignore[arg-type]
