"""구역 점검 단독 검증 — `ZoneInspector` (FR-8 · ADR-41 · ADR-42).

틱을 거치는 시나리오(도착·기준 등록·반출·반입·판독 두 번 확정·기준 재등록)는
`test_runtime.py`·`test_command_api.py` 에 있다. 여기서는 런타임 없이 닿기 어려운 분기만
본다 — 낡은 위치의 방향 맞추기, 크기 없는 프레임, 워커 중재, 워커 사망, 두 번째 판독 거절.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from host.behavior.actions import register_actions
from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.zone_inspector import ZoneInspector
from host.behavior.zones import Zone
from host.vision.vlm_reader import Answer, Reading

T0 = 1_000_000
#: 공장 모드의 선행 기능 검사를 연다 — 구역 점검은 공장 모드에서만 돈다.
pytestmark = pytest.mark.usefixtures("unlock_modes")


def _reading(values: dict) -> Reading:
    return Reading(
        answers=tuple(Answer(k, v, "yes" if v else "no", 1) for k, v in values.items()),
        degraded=False,
        reason=None,
        taken_at_ms=0,
    )


def _frame(*, width: int = 640, height: int = 480) -> SimpleNamespace:
    """사람도 물건도 없는 프레임."""
    return SimpleNamespace(detections=(), jpeg=b"zone", frame_width=width, frame_height=height)


class SlotVlm:
    """`VlmWorker` 대역 — `slot` 에 넣어 둔 것이 `take()` 에 나오고, `refuse` 면 걸지 않는다."""

    def __init__(self) -> None:
        self.available = True
        self.busy = False
        self.refuse = False
        self.slot: Reading | None = None
        self.submitted: list[int] = []

    def submit(self, _image, *, now_ms: int, **_keys) -> bool:
        if self.refuse:
            return False
        self.submitted.append(now_ms)
        return True

    def take(self) -> Reading | None:
        reading, self.slot = self.slot, None
        return reading


@pytest.fixture
def parts(cfg: dict, tmp_path: Path):
    """순찰 중인 공장 모드의 점검기 하나. 구역 A 는 원점에서 `yaw` 0 을 바라본다."""
    config = deepcopy(cfg)
    config["change_detect"]["snapshot_dir"] = str(tmp_path / "snapshots")
    config["change_detect"]["vlm_hazards"] = True
    behavior = behavior_from_config(Commander(), config)
    register_actions(behavior, config)  # 방향 맞추기가 지시를 넣는 `ZONE_INSPECT` 시퀀스
    behavior.event(Event.START_PATROL, now_ms=T0)
    vlm = SlotVlm()
    fall = SimpleNamespace(waiting=False, suspected=[])
    fall.suspect = lambda source, _now_ms, **_kw: fall.suspected.append(source)
    records: list[str] = []

    def apply(event: Event, now_ms: int) -> bool:
        return behavior.event(event, now_ms=now_ms)

    inspector = ZoneInspector(
        config,
        behavior=behavior,
        mission=Mission(config, mode="factory"),
        vlm=vlm,
        fall=fall,
        anchors=(Zone("A", 0.0, 0.0, 0.0),),
        apply=apply,
        record=lambda kind, *_: records.append(kind),
    )
    return SimpleNamespace(
        inspector=inspector,
        behavior=behavior,
        vlm=vlm,
        fall=fall,
        records=records,
        pose_timeout_ms=int(config["localization"]["pose_timeout_ms"]),
    )


def _arrive(parts, now_ms: int = T0) -> None:
    parts.inspector.note_pose((0.0, 0.0, 0.0), now_ms)
    parts.inspector.inspect(_frame(), now_ms)
    assert parts.behavior.state == "ZONE_INSPECT"


def test_a_stale_pose_while_aligning_stops_and_reads_nothing(parts) -> None:
    """방향을 맞추는 중 위치가 낡으면 돌지 않고 서서 기다린다 — 장면도 읽지 않는다."""
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + parts.pose_timeout_ms + 1)
    assert parts.vlm.submitted == []
    assert parts.behavior.state == "ZONE_INSPECT"


def test_a_frame_without_a_size_is_not_read(parts) -> None:
    """크기를 모르는 프레임은 기준과 견줄 수 없다 — 판독도 걸지 않는다."""
    _arrive(parts)
    parts.inspector.inspect(_frame(width=0), T0 + 100)
    assert parts.vlm.submitted == []
    parts.inspector.inspect(_frame(), T0 + 200)
    assert parts.vlm.submitted == [T0 + 200]


def test_the_zone_reading_waits_for_the_fall_reading(parts) -> None:
    """워커는 하나다 — 쓰러짐 판독이 도는 동안에는 걸지 않고 다음 프레임에 건다."""
    _arrive(parts)
    parts.fall.waiting = True
    parts.inspector.inspect(_frame(), T0 + 100)
    assert parts.vlm.submitted == []
    assert not parts.inspector.waiting
    parts.fall.waiting = False
    parts.inspector.inspect(_frame(), T0 + 200)
    assert parts.vlm.submitted == [T0 + 200]
    assert parts.inspector.waiting


def test_a_dead_worker_leaves_nothing_behind(parts) -> None:
    """판독 스레드가 죽어 결과가 없으면 기록도 의심도 없이 기다림만 푼다."""
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + 100)
    assert parts.inspector.waiting
    parts.vlm.slot = None
    parts.inspector.inspect(_frame(), T0 + 200)
    assert not parts.inspector.waiting
    assert parts.records == []
    assert parts.fall.suspected == []


def test_a_refused_second_reading_confirms_nothing(parts) -> None:
    """첫 판독 «예» 뒤 두 번째 판독을 걸지 못하면 의심만 남기고 확정하지 않는다."""
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + 100)
    parts.vlm.slot = _reading({"fallen_object": True, "person_down": True})
    parts.vlm.refuse = True
    parts.inspector.inspect(_frame(), T0 + 200)
    assert not parts.inspector.waiting
    assert parts.records == ["zone_reading"]
    assert parts.fall.suspected == ["zone_vlm"], "person_down «예» 는 의심으로 넘긴다"
    assert parts.vlm.submitted == [T0 + 100]
