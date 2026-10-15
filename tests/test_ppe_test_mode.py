"""PPE 시험 모드: 이동·모델·현장 포트 없이 프레임 기록과 쓰러짐 교차검증을 확인한다."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from host.behavior.commander import Commander
from host.behavior.escalation import Escalation
from host.behavior.fall_monitor import FallMonitor
from host.behavior.fsm import Event, behavior_from_config
from host.behavior.mission import Mission
from host.dashboard.server import encode_vision_frame
from host.dashboard.state import DashboardState
from host.runtime import Runtime
from host.runtime_cli import build_parser
from host.vision.detector import Detection
from host.vision.person import FallenVerdict, Sighting
from host.vision.ppe_detector import PpeDetector, ppe_payload
from host.vision.stream_client import Frame
from host.vision.tracker import Track
from host.vision.vlm_reader import Answer, Reading
from host.vision.worker import VisionResult, VisionWorker

pytestmark = pytest.mark.usefixtures("unlock_modes")


@pytest.fixture
def cfg(cfg: dict) -> dict:
    # 세션 공용 설정을 test_mode 로 바꾸면 뒤 시험 런타임이 전부 거부된다. 사본만 고친다.
    return copy.deepcopy(cfg)


class Vlm:
    busy = False
    slot = None

    def __init__(self):
        self.requests = []

    def submit(self, image, **kwargs):
        self.requests.append((image, kwargs))
        return True

    def take(self):
        value, self.slot = self.slot, None
        return value


def frame():
    return VisionResult(
        detections=(),
        jpeg=b"same-jpeg",
        frame_seq=1,
        frame_width=640,
        frame_height=480,
        frame_received_ms=100,
        completed_ms=100,
        inference_ms=1,
        sighting=Sighting(False, False, 0, 0, None, None),
        tracks=(),
        markers=(),
        fallen=FallenVerdict(True, True, True, 3.09, 3023),
    )


@pytest.mark.parametrize("state", ["IDLE", "FAILSAFE"])
@pytest.mark.parametrize("value", [True, False, None])
def test_rule_cross_verification_at_rest(cfg, state, value):
    cfg["vision"]["ppe"]["test_mode"] = True
    behavior = behavior_from_config(Commander(), cfg)
    if state == "FAILSAFE":
        behavior.event(Event.ESTOP, now_ms=0)
    original = behavior.state
    vlm = Vlm()
    records, applied = [], []
    monitor = FallMonitor(
        cfg,
        behavior=behavior,
        mission=Mission(cfg, mode="factory"),
        escalation=Escalation(cfg),
        vlm=vlm,
        apply=lambda event, at: applied.append((event, at)) or False,
        record=lambda *args: records.append(args),
        zone_waiting=lambda: False,
    )
    monitor.ask(frame(), 100)
    assert vlm.requests[0][0] == b"same-jpeg"
    assert vlm.requests[0][1]["keys"] == ("person_down",)
    if value is None:
        vlm.busy = True
        monitor.watch(100 + cfg["vision"]["vlm"]["budget_ms"])
    else:
        vlm.slot = Reading(
            (Answer("person_down", value, "yes" if value else "no", 12),), False, None, 100
        )
        monitor.watch(120)
    assert len(records) == 1
    kind, _, judgement = records[0]
    assert kind == ("person_fallen" if value is True else "fall_review_required")
    assert judgement["rule_yes"] is True and judgement["vlm"] is value
    assert judgement["latency_ms"] >= 20
    assert behaviour_unchanged(behavior, original, applied)
    if value is None:
        vlm.busy = False
        vlm.slot = Reading((Answer("person_down", True, "yes", 12),), False, None, 100)
        monitor.watch(4000)
        assert len(records) == 1, "타임아웃 후 늦은 yes는 확정하지 않는다"


def behaviour_unchanged(behavior, original, applied):
    return behavior.state == original and applied == []


def test_track_regions_and_ws_use_original_image_coordinates(cfg):
    class Model:
        def detect(self, _image):
            return [
                Detection("no_helmet", 0.9, (10, 30, 30, 50)),
                Detection("vest", 0.8, (10, 70, 60, 160)),
            ]

    detector = PpeDetector(cfg, Model())
    tracks = (Track(1, (100, 50, 200, 300), 0.9, 0), Track(2, (300, 0, 400, 300), 0.9, 0))
    results = detector.observe_all(np.zeros((480, 640, 3), np.uint8), tracks, 100)
    assert results[0].regions[0].label == "no_helmet"
    assert results[0].regions[0].box == (102, 60, 122, 80)
    assert results[1].clipped and all(r.label == "UNDETERMINED" for r in results[1].regions)
    result = replace(frame(), ppe=results[0], ppe_tracks=results, tracks=tracks)
    packet = encode_vision_frame(result)
    length = int.from_bytes(packet[:4], "big")
    header = json.loads(packet[4 : 4 + length])
    assert header["ppe"][0]["regions"][0]["box"] == [102, 60, 122, 80]
    assert header["ppe"][1]["reason"] == "머리 클리핑"
    assert packet[4 + length :] == result.jpeg
    assert len(ppe_payload(result)) == 2


def test_every_worker_frame_reaches_sink_even_when_latest_is_overwritten(cfg, monkeypatch):
    cfg["vision"]["ppe"]["test_mode"] = True
    worker = VisionWorker(
        cfg, detector=SimpleNamespace(detect=lambda _: []), reader=None, clock=lambda: 100
    )
    monkeypatch.setattr(
        "host.vision.worker.decode_jpeg", lambda _: np.zeros((480, 640, 3), np.uint8)
    )
    seen = []
    worker.set_result_sink(lambda result: seen.append(result.frame_seq))
    for seq in range(3):
        worker._run_one(Frame(seq=seq, received_ms=100, payload=b"jpeg"), 100)
    assert seen == [0, 1, 2]
    assert worker.latest().frame_seq == 2


def test_ppe_debug_record_is_display_only(cfg, clock):
    cfg["vision"]["ppe"]["test_mode"] = True
    records = []
    recorder = SimpleNamespace(record=lambda *args, **kwargs: records.append((args, kwargs)))
    board = DashboardState("mechdog-01", stale_after_ms=3000)
    runtime = Runtime(
        cfg,
        device_id="mechdog-01",
        clock=clock,
        motion_lock=True,
        mission=Mission(cfg, mode="factory"),
        recorder=recorder,
        dashboard=board,
    )
    runtime.vision_recording.record_ppe_frame(frame())
    assert records[0][0] == ("ppe_debug",)
    assert records[0][1]["frame_seq"] == 1
    assert runtime.behavior.state == "IDLE"
    assert build_parser().parse_args(["--device", "mechdog-01", "--ppe-test"]).ppe_test
    runtime.incidents.record_scene(
        "fall_review_required",
        frame(),
        {
            "rule_yes": True,
            "vlm": False,
            "test_mode": True,
            "reason": "disagreement",
        },
    )
    assert records[-1][0] == ("fall_cross_result",)
    events, _ = board.events_since(0)
    assert events[-1]["event"] == "fall_review_required"
    assert events[-1]["judgement"]["vlm"] is False


def test_unavailable_vlm_releases_a_review_without_new_frames(cfg):
    cfg["vision"]["ppe"]["test_mode"] = True
    vlm = Vlm()
    vlm.submit = lambda *_args, **_kwargs: False
    records = []
    monitor = FallMonitor(
        cfg,
        behavior=behavior_from_config(Commander(), cfg),
        mission=Mission(cfg, mode="factory"),
        escalation=Escalation(cfg),
        vlm=vlm,
        apply=lambda *_: False,
        record=lambda *args: records.append(args),
        zone_waiting=lambda: False,
    )
    monitor.ask(frame(), 100)
    monitor.watch(100 + cfg["vision"]["vlm"]["budget_ms"])
    assert records[0][0] == "fall_review_required"
    assert records[0][2]["reason"] == "vlm_unavailable_or_busy"
