"""구역 점검 단독 검증 — `ZoneInspector` (FR-8 · ADR-41 · ADR-42).

틱을 거치는 시나리오(도착·기준 등록·반출·반입·판독 두 번 확정·기준 재등록)는
`test_runtime.py`·`test_command_api.py` 에 있다. 여기서는 런타임 없이 닿기 어려운 분기만
본다 — 낡은 위치의 방향 맞추기, 크기 없는 프레임, 워커 중재, 워커 사망, 두 번째 판독 거절,
화기 위험구역(`zones.hazard_ids`)의 위험물 가벼운 경고.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from host.behavior.actions import register_actions
from host.behavior.change_detect import Change, ChangeKind
from host.behavior.commander import Commander
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.behavior.zone_inspector import ZONE_KEYS, ZoneInspector
from host.behavior.zones import Zone
from host.vision.vlm_reader import Answer, Reading

T0 = 1_000_000
#: 공장 모드의 선행 기능 검사를 연다 — 구역 점검은 공장 모드에서만 돈다.
pytestmark = pytest.mark.usefixtures("unlock_modes")


def _reading(values: dict, *, degraded: bool = False) -> Reading:
    return Reading(
        answers=tuple(Answer(k, v, "yes" if v else "no", 1) for k, v in values.items()),
        degraded=degraded,
        reason="budget_exhausted" if degraded else None,
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
        #: 판독마다 물은 항목 — `None` 이면 전부다.
        self.keys: list[tuple[str, ...] | None] = []

    def submit(self, _image, *, now_ms: int, keys=None) -> bool:
        if self.refuse:
            return False
        self.submitted.append(now_ms)
        self.keys.append(None if keys is None else tuple(keys))
        return True

    def take(self) -> Reading | None:
        reading, self.slot = self.slot, None
        return reading


def _build(cfg: dict, tmp_path: Path, *, zone: str = "A", **change) -> SimpleNamespace:
    """순찰 중인 공장 모드의 점검기 하나. 구역 `zone` 은 원점에서 `yaw` 0 을 바라본다."""
    config = deepcopy(cfg)
    config["change_detect"]["snapshot_dir"] = str(tmp_path / "snapshots")
    config["change_detect"]["vlm_hazards"] = True
    config["change_detect"].update(change)
    behavior = behavior_from_config(Commander(), config)
    register_actions(behavior, config)  # 방향 맞추기가 지시를 넣는 `ZONE_INSPECT` 시퀀스
    behavior.event(Event.START_PATROL, now_ms=T0)
    vlm = SlotVlm()
    fall = SimpleNamespace(waiting=False, suspected=[])
    fall.suspect = lambda source, _now_ms, **_kw: fall.suspected.append(source)
    records: list[str] = []
    payloads: list[dict] = []

    def record(kind: str, _frame, payload: dict) -> None:
        records.append(kind)
        payloads.append(payload)

    def apply(event: Event, now_ms: int) -> bool:
        return behavior.event(event, now_ms=now_ms)

    inspector = ZoneInspector(
        config,
        behavior=behavior,
        mission=Mission(config, mode="factory"),
        vlm=vlm,
        fall=fall,
        anchors=(Zone(zone, 0.0, 0.0, 0.0),),
        apply=apply,
        record=record,
    )
    return SimpleNamespace(
        inspector=inspector,
        behavior=behavior,
        vlm=vlm,
        fall=fall,
        records=records,
        payloads=payloads,
        pose_timeout_ms=int(config["localization"]["pose_timeout_ms"]),
        visit_frames=int(config["change_detect"]["visit_frames"]),
    )


@pytest.fixture
def parts(cfg: dict, tmp_path: Path):
    """구역 A(화기 위험구역 아님)의 점검기."""
    return _build(cfg, tmp_path)


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


# ── 화기 위험구역 (`zones.hazard_ids` · `change_detect.vlm_hazard_items`) ──────────


def _visit(parts, *readings: Reading) -> None:
    """도착해 한 방문을 끝까지 돈다. 판독은 걸린 순서대로 `readings` 를 돌려준다.

    틱마다 사람 없는 프레임 하나가 모이고, 판독은 건 다음 틱에 줍는다.
    """
    _arrive(parts)
    queue = list(readings)
    now = T0
    for _ in range(parts.visit_frames + len(readings) + 2):
        now += 100
        if parts.inspector.waiting:
            parts.vlm.slot = queue.pop(0) if queue else _reading({})
        parts.inspector.inspect(_frame(), now)
        if parts.behavior.state != "ZONE_INSPECT":
            return


def test_the_demo_settings_mark_zone_c_as_the_hazard_zone(cfg: dict) -> None:
    """시연 설정은 C 하나만 화기 위험구역이고 경고 스위치는 켜져 있다 (L3 스위치는 꺼짐)."""
    assert cfg["zones"]["hazard_ids"] == ["C"]
    assert cfg["change_detect"]["vlm_hazard_items"] is True
    assert cfg["change_detect"]["vlm_hazards"] is False


def test_hazard_item_yes_twice_is_one_light_notice(cfg: dict, tmp_path: Path) -> None:
    """화기 위험구역에서 두 판독이 모두 «예» 면 가벼운 경고 하나 — L3 없이 순찰을 잇는다."""
    parts = _build(cfg, tmp_path, zone="C", vlm_hazards=False)
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [(*ZONE_KEYS, "hazard_item")] * 2
    assert parts.records == ["zone_reading", "zone_reading", "hazard_notice"]
    assert parts.payloads[-1] == {"zone": "C", "items": ["hazard_item"], "source": "vlm"}
    assert parts.behavior.state == "PATROL", "ZONE_CLEAR 로 순찰에 돌아간다"
    assert not parts.inspector.alarm_alert


def test_hazard_item_yes_then_no_confirms_nothing(cfg: dict, tmp_path: Path) -> None:
    """두 번째 판독이 «아니오» 면 확정하지 않는다."""
    parts = _build(cfg, tmp_path, zone="C")
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": False}))
    assert len(parts.vlm.submitted) == 2
    assert "hazard_notice" not in parts.records
    assert parts.behavior.state == "PATROL"


def test_a_degraded_second_reading_confirms_no_hazard_item(cfg: dict, tmp_path: Path) -> None:
    """저하된 두 번째 판독은 «빈 채널» 이다 — «예» 가 담겨 있어도 확정하지 않는다."""
    parts = _build(cfg, tmp_path, zone="C")
    _visit(
        parts,
        _reading({"hazard_item": True}),
        _reading({"hazard_item": True}, degraded=True),
    )
    assert len(parts.vlm.submitted) == 2
    assert "hazard_notice" not in parts.records
    assert parts.behavior.state == "PATROL"


def test_hazard_items_switched_off_never_confirm(cfg: dict, tmp_path: Path) -> None:
    """스위치를 끄면 «예» 라도 다시 묻지도 확정하지도 않는다 (묻기와 기록은 남는다)."""
    parts = _build(cfg, tmp_path, zone="C", vlm_hazards=False, vlm_hazard_items=False)
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [(*ZONE_KEYS, "hazard_item")], "두 번째 판독을 걸지 않는다"
    assert parts.records == ["zone_reading"]
    assert parts.behavior.state == "PATROL"


def test_a_plain_zone_never_asks_for_hazard_items(cfg: dict, tmp_path: Path) -> None:
    """화기 위험구역이 아니면 `hazard_item` 을 묻지 않는다 — 판독마다 시간이 붙는다."""
    parts = _build(cfg, tmp_path, zone="A")
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [ZONE_KEYS], "«예» 가 와도 이 구역에서는 보지 않는다"
    assert "hazard_notice" not in parts.records


def test_the_l3_hazard_path_is_unchanged_at_a_hazard_zone(cfg: dict, tmp_path: Path) -> None:
    """화기 위험구역에서도 넘어짐 두 번 «예» 는 그대로 `zone_changed`(L3) 이다."""
    parts = _build(cfg, tmp_path, zone="C")
    _visit(parts, _reading({"fallen_object": True}), _reading({"fallen_object": True}))
    assert parts.records == ["zone_reading", "zone_reading", "zone_changed"]
    assert parts.behavior.state == "ALERT"
    assert parts.inspector.alarm_alert


def test_l3_and_hazard_item_in_one_visit_leave_both(cfg: dict, tmp_path: Path) -> None:
    """같은 방문에서 둘 다 확정하면 가벼운 경고를 먼저 남기고 L3 로 간다."""
    parts = _build(cfg, tmp_path, zone="C")
    both = {"fallen_object": True, "hazard_item": True}
    _visit(parts, _reading(both), _reading(both))
    assert parts.records == ["zone_reading", "zone_reading", "hazard_notice", "zone_changed"]
    changes = parts.payloads[-1]["changes"]
    assert changes == [{"kind": "fallen_object", "source": "vlm"}], "L3 에 위험물을 섞지 않는다"
    assert parts.behavior.state == "ALERT"


def test_a_confirmed_object_change_still_waits_for_the_hazard_reading(
    cfg: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """물건 변화를 확정한 방문도 남은 판독을 기다린다 — 안 기다리면 위험물 경고가 사라진다.

    시연 둘째 바퀴의 C 는 놓아 둔 라이터·보조배터리가 «반입» 으로도 확정될 수 있다.
    그때 두 번째 «예» 가 늦게 오면 방문이 먼저 끝나 `hazard_notice` 를 잃었다.
    """
    parts = _build(cfg, tmp_path, zone="C", vlm_hazards=False)
    inspector = parts.inspector
    inspector._baselines.register("C", (), frame_size=(640, 480), now_ms=T0, jpeg=b"")
    added = Change(ChangeKind.ADDED, "cell phone", 1, None)
    monkeypatch.setattr(inspector._confirmer, "observe", lambda _zone, _observed: (added,))
    _arrive(parts)
    now = T0 + 100
    inspector.inspect(_frame(), now)  # 첫 판독을 건다
    now += 100
    parts.vlm.slot = _reading({"hazard_item": True})
    inspector.inspect(_frame(), now)  # «예» → 두 번째 판독을 건다
    assert len(parts.vlm.submitted) == 2
    parts.vlm.busy = True  # 두 번째 판독이 늦는다
    for _ in range(parts.visit_frames):
        now += 100
        inspector.inspect(_frame(), now)
    assert parts.behavior.state == "ZONE_INSPECT", "판독이 남았으면 방문을 끝내지 않는다"
    parts.vlm.busy = False
    parts.vlm.slot = _reading({"hazard_item": True})
    now += 100
    inspector.inspect(_frame(), now)
    assert "hazard_notice" in parts.records
    assert parts.behavior.state == "PATROL"
