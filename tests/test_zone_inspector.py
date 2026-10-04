"""구역 점검 단독 검증 — `ZoneInspector` (FR-8 · ADR-41 · ADR-42).

틱을 거치는 시나리오(도착·판독 두 번 확정)는 `test_runtime.py` 에 있다. 여기서는 런타임 없이 닿기 어려운 분기만
본다 — 낡은 위치의 방향 맞추기, 크기 없는 프레임, 워커 중재, 워커 사망, 두 번째 판독 거절,
화기 위험구역(`zones.hazard_ids`)의 위험물 가벼운 경고.
"""

from __future__ import annotations

import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

from host.behavior.actions import register_actions
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


def _build(
    cfg: dict,
    *,
    zone: str = "A",
    yaw: float | None = 0.0,
    aim_deg: float | None = None,
    ready_to_inspect=None,
    **change,
) -> SimpleNamespace:
    """순찰 중인 공장 모드의 점검기 하나. 구역 `zone` 은 원점에서 `yaw` 0 을 바라본다."""
    config = deepcopy(cfg)
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
        anchors=(Zone(zone, 0.0, 0.0, yaw, aim_deg=aim_deg),),
        apply=apply,
        record=record,
        ready_to_inspect=ready_to_inspect,
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
def parts(cfg: dict):
    """구역 A(화기 위험구역 아님)의 점검기."""
    return _build(cfg)


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


def test_camera_aim_waits_for_navigation_before_entering_inspection(cfg) -> None:
    ready = {"value": False}
    parts = _build(cfg, aim_deg=90.0, ready_to_inspect=lambda _zone, _now: ready["value"])
    inspector = parts.inspector
    inspector.note_pose((0.0, 0.0, 0.0), T0)
    assert inspector.awaits_inspection("A", T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "PATROL"
    assert parts.vlm.submitted == []
    assert inspector._visit_seen == []
    ready["value"] = True
    inspector.note_pose((0.0, 0.0, math.pi / 2), T0 + 100)
    inspector.inspect(_frame(), T0 + 100)
    assert parts.behavior.state == "ZONE_INSPECT"
    assert inspector._aligned, "길 찾기가 이미 맞춘 방향을 기존 yaw로 다시 돌리지 않는다"
    assert not inspector.awaits_inspection("A", T0 + 100)
    inspector.inspect(_frame(), T0 + 200)
    assert parts.vlm.submitted == [T0 + 200]


def test_legacy_zone_does_not_wait_for_navigation(cfg) -> None:
    parts = _build(cfg, ready_to_inspect=lambda *_args: False)
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + 100)
    assert parts.vlm.submitted == [T0 + 100]


def test_inspector_waits_only_when_a_fresh_pose_can_reach_the_anchor(cfg) -> None:
    parts = _build(cfg, aim_deg=90.0)
    inspector = parts.inspector
    assert not inspector.awaits_inspection("A", T0)
    inspector.note_pose((0.4, 0.0, 0.0), T0)
    assert not inspector.awaits_inspection("A", T0)
    inspector.note_pose((0.0, 0.0, 0.0), T0)
    assert inspector.awaits_inspection("A", T0)
    assert not inspector.awaits_inspection("B", T0)
    assert not inspector.awaits_inspection("A", T0 + parts.pose_timeout_ms + 1)


@pytest.mark.parametrize("facing", [0.0, math.pi / 2])
def test_camera_aim_without_navigation_never_turns_or_inspects(cfg, facing) -> None:
    parts = _build(cfg, aim_deg=90.0)
    inspector = parts.inspector
    inspector.note_pose((0.0, 0.0, facing), T0)
    inspector.inspect(_frame(), T0)
    assert parts.behavior.state == "PATROL", "항법의 안전 관문 없이 점검 정렬을 시작하지 않는다"
    inspector.inspect(_frame(), T0 + 100)
    commander = Commander()
    inspector._turn(commander, T0 + 100)
    assert commander.intent.fields == {"step": 0.0, "angle": 0.0}
    assert parts.vlm.submitted == []
    # PATROL의 온보드 장애물 반응도 점검 전이로 가로채지 않는다.
    parts.behavior.event(Event.ONBOARD_AVOID, now_ms=T0 + 200)
    assert parts.behavior.state == "AVOID"
    inspector.inspect(_frame(), T0 + 200)
    assert parts.vlm.submitted == []


def test_legacy_yaw_without_navigation_still_aligns(cfg) -> None:
    parts = _build(cfg, yaw=math.pi / 2)
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + 100)
    commander = Commander()
    parts.inspector._turn(commander, T0 + 100)
    assert commander.intent.fields == {"step": 0.0, "angle": cfg["zones"]["align_turn_deg"]}
    assert parts.vlm.submitted == []


def test_a_frame_without_a_size_is_not_read(parts) -> None:
    """크기를 모르는 프레임은 온전한 프레임이 아니다 — 판독을 걸지 않는다."""
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


def test_hazard_item_yes_twice_is_one_light_notice(cfg: dict) -> None:
    """화기 위험구역에서 두 판독이 모두 «예» 면 가벼운 경고 하나 — L3 없이 순찰을 잇는다."""
    parts = _build(cfg, zone="C", vlm_hazards=False)
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [(*ZONE_KEYS, "hazard_item")] * 2
    assert parts.records == ["zone_reading", "zone_reading", "hazard_notice"]
    assert parts.payloads[-1] == {"zone": "C", "items": ["hazard_item"], "source": "vlm"}
    assert parts.behavior.state == "PATROL", "ZONE_CLEAR 로 순찰에 돌아간다"
    assert not parts.inspector.alarm_alert


def test_hazard_item_yes_then_no_confirms_nothing(cfg: dict) -> None:
    """두 번째 판독이 «아니오» 면 확정하지 않는다."""
    parts = _build(cfg, zone="C")
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": False}))
    assert len(parts.vlm.submitted) == 2
    assert "hazard_notice" not in parts.records
    assert parts.behavior.state == "PATROL"


def test_a_degraded_second_reading_confirms_no_hazard_item(cfg: dict) -> None:
    """저하된 두 번째 판독은 «빈 채널» 이다 — «예» 가 담겨 있어도 확정하지 않는다."""
    parts = _build(cfg, zone="C")
    _visit(
        parts,
        _reading({"hazard_item": True}),
        _reading({"hazard_item": True}, degraded=True),
    )
    assert len(parts.vlm.submitted) == 2
    assert "hazard_notice" not in parts.records
    assert parts.behavior.state == "PATROL"


def test_hazard_items_switched_off_never_confirm(cfg: dict) -> None:
    """스위치를 끄면 «예» 라도 다시 묻지도 확정하지도 않는다 (묻기와 기록은 남는다)."""
    parts = _build(cfg, zone="C", vlm_hazards=False, vlm_hazard_items=False)
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [(*ZONE_KEYS, "hazard_item")], "두 번째 판독을 걸지 않는다"
    assert parts.records == ["zone_reading"]
    assert parts.behavior.state == "PATROL"


def test_a_plain_zone_never_asks_for_hazard_items(cfg: dict) -> None:
    """화기 위험구역이 아니면 `hazard_item` 을 묻지 않는다 — 판독마다 시간이 붙는다."""
    parts = _build(cfg, zone="A")
    _visit(parts, _reading({"hazard_item": True}), _reading({"hazard_item": True}))
    assert parts.vlm.keys == [ZONE_KEYS], "«예» 가 와도 이 구역에서는 보지 않는다"
    assert "hazard_notice" not in parts.records


def test_the_l3_hazard_path_is_unchanged_at_a_hazard_zone(cfg: dict) -> None:
    """화기 위험구역에서도 넘어짐 두 번 «예» 는 그대로 `zone_changed`(L3) 이다."""
    parts = _build(cfg, zone="C")
    _visit(parts, _reading({"fallen_object": True}), _reading({"fallen_object": True}))
    assert parts.records == ["zone_reading", "zone_reading", "zone_changed"]
    assert parts.behavior.state == "ALERT"
    assert parts.inspector.alarm_alert


def test_l3_and_hazard_item_in_one_visit_leave_both(cfg: dict) -> None:
    """같은 방문에서 둘 다 확정하면 가벼운 경고를 먼저 남기고 L3 로 간다."""
    parts = _build(cfg, zone="C")
    both = {"fallen_object": True, "hazard_item": True}
    _visit(parts, _reading(both), _reading(both))
    assert parts.records == ["zone_reading", "zone_reading", "hazard_notice", "zone_changed"]
    changes = parts.payloads[-1]["changes"]
    assert changes == [{"kind": "fallen_object", "source": "vlm"}], "L3 에 위험물을 섞지 않는다"
    assert parts.behavior.state == "ALERT"


def test_a_visit_with_all_frames_still_waits_for_the_hazard_reading(cfg: dict) -> None:
    """프레임을 다 모은 방문도 남은 판독을 기다린다 — 안 기다리면 위험물 경고가 사라진다.

    두 번째 «예» 가 늦게 오면 방문이 먼저 끝나 `hazard_notice` 를 잃는다.
    """
    parts = _build(cfg, zone="C", vlm_hazards=False)
    inspector = parts.inspector
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


def test_a_zone_visit_no_longer_reports_removed_or_added_objects(cfg: dict) -> None:
    """반출·반입 비교는 폐기했다 (2026-10-05 · WBS 3.6.1~3.6.3). 첫 방문에 있던 병이 뒤의
    연속 두 방문에 없어도 구역 점검은 판독만 남기고 `zone_notice`·`zone_changed` 를 내지 않는다."""
    parts = _build(cfg)
    bottle = SimpleNamespace(label="bottle", box=(500.0, 10.0, 600.0, 100.0), score=0.9)
    now = T0
    for scene in ((bottle,), (), ()):
        frame = SimpleNamespace(detections=scene, jpeg=b"zone", frame_width=640, frame_height=480)
        parts.inspector.note_pose((0.0, 0.0, 0.0), now)
        parts.inspector.inspect(frame, now)
        assert parts.behavior.state == "ZONE_INSPECT"
        for _ in range(parts.visit_frames + 2):
            now += 100
            if parts.inspector.waiting:
                parts.vlm.slot = _reading({})
            parts.inspector.inspect(frame, now)
        assert parts.behavior.state == "PATROL"
        # 앵커 반경의 두 배 밖으로 나가야 다음에 같은 구역을 다시 점검한다.
        now += 100
        parts.inspector.note_pose((10.0, 0.0, 0.0), now)
        parts.inspector.inspect(frame, now)
    assert parts.records == ["zone_reading"] * 3


# ── 구역 안 통로 막힘 (`blocked_path`) — L3 가 아니라 가벼운 경고 `path_blocked` ──────


def test_blocked_path_yes_twice_is_one_light_notice(cfg: dict) -> None:
    """ADR-41 개정(2026-10-01): 통로 막힘 확정은 L3 없이 `path_blocked`(source vlm) 이다."""
    parts = _build(cfg)
    _visit(parts, _reading({"blocked_path": True}), _reading({"blocked_path": True}))
    assert parts.records == ["zone_reading", "zone_reading", "path_blocked"]
    assert parts.payloads[-1] == {"zone": "A", "source": "vlm"}
    assert parts.behavior.state == "PATROL"
    assert not parts.inspector.alarm_alert


def test_blocked_path_and_fallen_object_leave_the_notice_then_l3(cfg: dict) -> None:
    parts = _build(cfg)
    both = {"fallen_object": True, "blocked_path": True}
    _visit(parts, _reading(both), _reading(both))
    assert parts.records == ["zone_reading", "zone_reading", "path_blocked", "zone_changed"]
    assert parts.payloads[-1]["changes"] == [{"kind": "fallen_object", "source": "vlm"}]
    assert parts.behavior.state == "ALERT"


def test_blocked_path_switched_off_never_confirms(cfg: dict) -> None:
    parts = _build(cfg, vlm_hazards=False)
    _visit(parts, _reading({"blocked_path": True}), _reading({"blocked_path": True}))
    assert "path_blocked" not in parts.records


# ── 판독을 기다리다 방문 밖으로 밀려나도 확정한 결론은 남는다 ─────────────────────────


def _waiting_for_second_reading(parts, first: Reading) -> int:
    """프레임을 다 모으고 두 번째 판독을 기다리는 중까지 간다. 그때의 시각을 돌려준다."""
    inspector = parts.inspector
    _arrive(parts)
    now = T0 + 100
    inspector.inspect(_frame(), now)  # 첫 판독을 건다
    now += 100
    parts.vlm.slot = first
    inspector.inspect(_frame(), now)  # «예» → 두 번째 판독을 건다
    assert len(parts.vlm.submitted) == 2
    parts.vlm.busy = True  # 두 번째 판독이 늦는다
    for _ in range(parts.visit_frames):
        now += 100
        inspector.inspect(_frame(), now)
    assert parts.behavior.state == "ZONE_INSPECT"
    return now


def test_a_hazard_item_survives_a_reading_that_also_says_person_down(cfg: dict) -> None:
    parts = _build(cfg, zone="C", vlm_hazards=False)
    now = _waiting_for_second_reading(parts, _reading({"hazard_item": True}))
    parts.fall.suspect = lambda *_a, **_kw: parts.behavior.event(Event.FALL_SUSPECTED, now_ms=now)
    parts.vlm.busy = False
    parts.vlm.slot = _reading({"hazard_item": True, "person_down": True})
    parts.inspector.inspect(_frame(), now + 100)
    assert parts.behavior.state == "ALERT"
    assert "hazard_notice" in parts.records
    assert not parts.inspector.alarm_alert, "떠난 뒤에는 ZONE_CHANGED 를 걸지 않는다"


@pytest.mark.parametrize(
    ("answer", "record"),
    [
        ("hazard_item", "hazard_notice"),
        ("blocked_path", "path_blocked"),
        ("fallen_object", "zone_changed"),
    ],
)
def test_a_visit_cut_off_mid_collection_keeps_what_it_already_confirmed(
    cfg: dict, answer: str, record: str
) -> None:
    """프레임을 다 모으기 전에 방문이 끊겨도 이미 «예» 2회로 확정한 VLM 항목은 남긴다.

    2026-10-01 사용자 결정 — 확정분만 기록한다. 전이는 하지 않는다(이미 떠났다).
    """
    parts = _build(cfg, zone="C")
    inspector = parts.inspector
    _arrive(parts)
    now = T0 + 100
    inspector.inspect(_frame(), now)  # 첫 판독을 건다
    for _ in range(2):  # «예» → 두 번째 판독 → «예» (확정)
        now += 100
        parts.vlm.slot = _reading({answer: True})
        inspector.inspect(_frame(), now)
    assert record not in parts.records, "아직 방문 중이라 결론을 미룬다"
    assert parts.behavior.state == "ZONE_INSPECT"
    parts.behavior.event(Event.MANUAL_ON, now_ms=now)
    inspector.inspect(_frame(), now + 100)
    assert parts.records.count(record) == 1
    assert parts.payloads[parts.records.index(record)]["zone"] == "C"
    assert parts.behavior.state == "MANUAL", "떠난 방문은 전이를 걸지 않는다"
    inspector.inspect(_frame(), now + 200)
    assert parts.records.count(record) == 1, "한 번만 남긴다"


def test_a_visit_cut_off_before_any_confirmation_records_nothing(cfg: dict) -> None:
    parts = _build(cfg, zone="C")
    _arrive(parts)
    parts.inspector.inspect(_frame(), T0 + 100)  # 첫 판독을 건다
    parts.vlm.slot = _reading({"hazard_item": True})
    parts.inspector.inspect(_frame(), T0 + 200)  # «예» 한 번 — 아직 확정 아님
    parts.vlm.busy = True  # 두 번째 판독이 늦는다
    parts.behavior.event(Event.MANUAL_ON, now_ms=T0 + 200)
    parts.inspector.inspect(_frame(), T0 + 300)  # 떠난 것을 본다 — 확정한 것이 없다
    parts.vlm.busy = False
    parts.vlm.slot = _reading({"hazard_item": True})
    parts.inspector.inspect(_frame(), T0 + 400)  # 떠난 뒤 온 두 번째 «예»
    assert "hazard_notice" not in parts.records


def test_a_reading_that_arrives_after_leaving_confirms_nothing(cfg: dict) -> None:
    parts = _build(cfg, zone="C", vlm_hazards=False)
    now = _waiting_for_second_reading(parts, _reading({"hazard_item": True}))
    parts.behavior.event(Event.MANUAL_ON, now_ms=now)
    parts.inspector.inspect(_frame(), now + 100)
    parts.vlm.busy = False
    parts.vlm.slot = _reading({"hazard_item": True})
    parts.inspector.inspect(_frame(), now + 200)
    assert "hazard_notice" not in parts.records


# ── 위험물 검출기 (`vision.hazard` · ADR-43 대안 ⓐ 개정) ────────────────────────────


def _hazard_frame(*confirmed: str) -> SimpleNamespace:
    """워커가 위험물 판정을 실은 프레임. `confirmed` 가 비면 아무것도 확정하지 않았다."""
    from host.vision.detector import Detection
    from host.vision.hazard_detector import HazardVerdict

    boxes = tuple(Detection(label, 0.9, (10.0, 20.0, 60.0, 90.0)) for label in confirmed)
    frame = _frame()
    frame.hazard = HazardVerdict(detections=boxes, confirmed=confirmed)
    return frame


def _detector_visit(parts, frame, *readings: Reading, limit: int = 80) -> int:
    """도착해 방문이 끝날 때까지 `frame()` 을 넣는다. 끝난 시각을 돌려준다."""
    parts.inspector.note_hazard_detector(True)
    _arrive(parts)
    queue = list(readings)
    now = T0
    for _ in range(limit):
        now += 100
        if parts.inspector.waiting:
            parts.vlm.slot = queue.pop(0) if queue else _reading({})
        parts.inspector.inspect(frame(), now)
        if parts.behavior.state != "ZONE_INSPECT":
            return now
    raise AssertionError("방문이 끝나지 않았다")


def test_the_detector_confirms_a_hazard_in_the_hazard_zone(cfg: dict) -> None:
    """위험구역에서 검출기가 확정하면 가벼운 경고 `hazard_notice` 하나를 남긴다 (source `detector`)."""
    parts = _build(cfg, zone="C")
    _detector_visit(parts, lambda: _hazard_frame("lighter"))
    assert parts.records.count("hazard_notice") == 1
    notice = parts.payloads[parts.records.index("hazard_notice")]
    assert notice == {"zone": "C", "items": ["lighter"], "source": "detector", "vlm": False}
    assert parts.behavior.state == "PATROL", "가벼운 경고다 — 순찰을 잇는다"


def test_a_hazard_outside_the_hazard_zone_raises_nothing(cfg: dict) -> None:
    """금지구역이 아닌 곳에서는 위험물이 확정돼 실려 와도 경고하지 않는다 — 검출기도 켜지 않는다."""
    parts = _build(cfg, zone="A")
    parts.inspector.note_hazard_detector(True)
    _arrive(parts)
    parts.inspector.inspect(_hazard_frame("lighter"), T0 + 100)
    assert not parts.inspector.watching_hazards
    _detector_visit(parts, lambda: _hazard_frame("lighter", "powerbank"))
    assert "hazard_notice" not in parts.records


def test_the_detector_is_watched_only_while_inspecting_the_hazard_zone(cfg: dict) -> None:
    """켤 때는 위험구역 점검 중 방향을 맞춘 뒤뿐이다 — 순찰 중·점검이 끝난 뒤에는 끈다."""
    parts = _build(cfg, zone="C")
    assert not parts.inspector.watching_hazards, "순찰 중"
    parts.inspector.note_hazard_detector(True)
    _arrive(parts)
    assert not parts.inspector.watching_hazards, "방향을 맞추기 전"
    parts.inspector.inspect(_frame(), T0 + 100)
    assert parts.inspector.watching_hazards
    _detector_visit(parts, _frame)
    assert not parts.inspector.watching_hazards, "방문이 끝났다"


def test_the_hazard_zone_visit_lasts_one_confirm_window(cfg: dict) -> None:
    """사람 없는 프레임은 0.5초면 찬다 — 위험구역은 검출기의 확정 창만큼 머문다."""
    window = int(cfg["vision"]["hazard"]["confirm_window_ms"])
    plain = _build(cfg, zone="C")
    plain.inspector.note_hazard_detector(False)
    _arrive(plain)
    now = T0
    while plain.behavior.state == "ZONE_INSPECT":
        now += 100
        if plain.inspector.waiting:
            plain.vlm.slot = _reading({})
        plain.inspector.inspect(_frame(), now)
    without = now - T0
    with_detector = _detector_visit(_build(cfg, zone="C"), _frame) - T0
    assert without < window <= with_detector


def test_a_late_confirmation_inside_the_window_is_kept(cfg: dict) -> None:
    """창이 찰 무렵에야 확정돼도 방문 안이면 경고한다 — 떠난 뒤가 아니다."""
    parts = _build(cfg, zone="C")
    seen = {"n": 0}

    def frame():
        seen["n"] += 1
        return _hazard_frame("powerbank") if seen["n"] >= 12 else _hazard_frame()

    _detector_visit(parts, frame)
    notice = parts.payloads[parts.records.index("hazard_notice")]
    assert notice["items"] == ["powerbank"]


def test_detector_and_vlm_in_one_visit_leave_one_notice(cfg: dict) -> None:
    """둘 다 확정하면 방송이 두 번 나가지 않게 기록 하나로 합친다."""
    parts = _build(cfg, zone="C")
    _detector_visit(
        parts,
        lambda: _hazard_frame("lighter"),
        _reading({"hazard_item": True}),
        _reading({"hazard_item": True}),
    )
    assert parts.records.count("hazard_notice") == 1
    notice = parts.payloads[parts.records.index("hazard_notice")]
    assert notice["source"] == "detector" and notice["vlm"] is True


def test_a_switched_off_detector_is_ignored(cfg: dict) -> None:
    """`vision.hazard.enabled: false` 면 실려 온 판정도 버린다 — VLM 판독만 남는다."""
    config = deepcopy(cfg)
    config["vision"]["hazard"]["enabled"] = False
    parts = _build(config, zone="C")
    _detector_visit(parts, lambda: _hazard_frame("lighter"))
    assert "hazard_notice" not in parts.records
    assert not parts.inspector.watching_hazards
