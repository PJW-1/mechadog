"""PPE 실기 관찰 도구의 판정 로직 검증 (FR-9 · WBS 3.7.3 선행).

⚠️ **모델 파일 없이 돌아야 한다.** 가중치는 저장소에 없고(용량·라이선스) CI 에도
카메라가 없다. 그래서 검출기를 주입 가능한 자리에 두고 여기서는 가짜를 넣는다 —
검증 대상은 추론이 아니라 **판정 규칙**이다.

⚠️ **판정은 세 갈래이고 순서가 규칙의 핵심이다.** 확인불가를 먼저 가리지 않으면
근거가 없을 때도 위반이 되고, 그것은 L3 경보로 이어져 관리자 확인으로만 풀린다.
그 순서가 지켜지는지를 경계마다 확인한다.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.vision.detector import Detection  # noqa: E402
from tools import ppe_live_check as ppe  # noqa: E402


def det(label: str, box: tuple[float, float, float, float], score: float = 0.9) -> Detection:
    return Detection(label=label, score=score, box=box)


# ── 판정 세 갈래 (FR-9.2.1 · FR-9.3) ────────────────────────────────


def test_judge_returns_compliant_when_helmet_and_vest_are_visible() -> None:
    result = ppe.judge([det("helmet", (10, 20, 40, 50)), det("vest", (8, 60, 50, 120))], 8, True)
    assert result.state == ppe.STATE_OK
    assert result.reason == ""


def test_judge_returns_violation_when_a_violation_class_is_present() -> None:
    result = ppe.judge([det("helmet", (10, 20, 40, 50)), det("no_vest", (8, 60, 50, 120))], 8, True)
    assert result.state == ppe.STATE_VIOLATION


@pytest.mark.parametrize(
    ("detections", "reason"),
    [
        ([], "아무것도 미검출"),
        ([det("vest", (8, 60, 50, 120))], "머리 미검출"),
        ([det("helmet", (10, 20, 40, 50))], "몸통 미검출"),
    ],
)
def test_judge_defers_when_evidence_is_missing(detections: list[Detection], reason: str) -> None:
    """근거가 없으면 위반이 아니라 확인불가다.

    ⚠️ 여기서 위반을 내보내면 L3 가 뜨고 관리자 확인으로만 풀린다. 판정 실패도
    결과이므로 기록하고 다시 본다 (FR-9.2.4).
    """
    result = ppe.judge(detections, 8, True)
    assert result.state == ppe.STATE_UNKNOWN
    assert result.reason == reason


def test_judge_defers_when_the_head_box_touches_the_top_edge() -> None:
    """머리가 잘린 프레임에서는 안전모를 볼 수 없다 (FR-9.2.1 · DR-15)."""
    clipped = [det("no_helmet", (10, 4, 40, 50)), det("no_vest", (8, 60, 50, 120))]
    result = ppe.judge(clipped, 8, True)
    assert result.state == ppe.STATE_UNKNOWN
    assert result.reason == "머리 클리핑"


def test_clipping_rule_can_be_turned_off_for_comparison() -> None:
    clipped = [det("no_helmet", (10, 4, 40, 50)), det("no_vest", (8, 60, 50, 120))]
    assert ppe.judge(clipped, 8, False).state == ppe.STATE_VIOLATION


def test_head_margin_is_the_boundary_not_a_range() -> None:
    boxes = [det("helmet", (10, 9, 40, 50)), det("vest", (8, 60, 50, 120))]
    assert ppe.judge(boxes, 8, True).state == ppe.STATE_OK


# ── 위반 확정 시간 창 (FR-9.4 · ADR-25) ─────────────────────────────


def test_window_needs_the_required_hits_before_confirming() -> None:
    window = ppe.ViolationWindow(1500, 3)
    assert window.observe(True, 0) is False
    assert window.observe(True, 100) is False
    assert window.observe(True, 200) is True
    assert window.confirmed is True


def test_window_confirms_only_once_per_entry() -> None:
    window = ppe.ViolationWindow(1500, 3)
    for now in (0, 100, 200):
        window.observe(True, now)
    assert window.observe(True, 300) is False
    assert window.confirmed is True


def test_hits_outside_the_window_do_not_accumulate() -> None:
    """⚠️ 프레임 수가 아니라 시간이다. 느린 추론에서 조건이 저절로 느슨해지면 안 된다."""
    window = ppe.ViolationWindow(1000, 3)
    window.observe(True, 0)
    window.observe(True, 500)
    assert window.observe(True, 2000) is False
    assert window.hits == 1


def test_window_releases_only_when_it_is_completely_empty() -> None:
    """진입과 해제를 비대칭으로 둔다 — 같은 조건이면 경계에서 떨린다."""
    window = ppe.ViolationWindow(1000, 3)
    for now in (0, 100, 200):
        window.observe(True, now)
    window.observe(False, 900)
    assert window.confirmed is True
    window.observe(False, 5000)
    assert window.confirmed is False


def test_window_reset_makes_acceptance_segments_independent() -> None:
    window = ppe.ViolationWindow(8000, 3)
    for now in (0, 1000, 2000):
        window.observe(True, now)
    assert window.confirmed is True

    window.reset()

    assert window.confirmed is False
    assert window.hits == 0


def test_xiao_acceptance_plan_covers_required_postures() -> None:
    specs, step_s, orientations = ppe.load_acceptance_plan(ppe.DEFAULT_ACCEPTANCE_PLAN, "xiao")
    keys = {spec["key"] for spec in specs}
    assert {"standing-all", "crouching-all", "clipped-base", "pitch-up", "sit"} <= keys
    assert "back-off" not in keys, "출고 자세 단계에서 후진은 제외했다"
    assert step_s > 0
    assert orientations == ["정면", "우측", "후면", "좌측"]


def test_segment_report_separates_coverage_from_conditional_accuracy() -> None:
    specs = [
        {"key": "all", "condition": "전신", "wear": "전부 착용", "expected": ppe.STATE_OK},
        {
            "key": "clipped",
            "condition": "머리 잘림",
            "wear": "전부 착용",
            "expected": ppe.STATE_UNKNOWN,
        },
    ]
    log = ppe.SegmentLog(specs, 15, ["정면"])
    log.select("all")
    log.observe([ppe.STATE_OK, ppe.STATE_UNKNOWN])
    log.select("clipped")
    log.observe([ppe.STATE_UNKNOWN])
    lines = []

    ppe.segment_section(lines.append, log)

    text = "\n".join(lines)
    assert "판정 가능률 50%" in text
    assert "실효 성공률 67%" in text
    assert "조건부 정확도(확인불가 제외) 100%" in text
    assert "보류 일치율 100%" in text


# ── person 크롭 (MODEL_PLAN 1.6) ────────────────────────────────────


def test_crop_person_pads_and_reports_its_origin() -> None:
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    crop, origin = ppe.crop_person(image, (100.0, 50.0, 200.0, 150.0), 0.1)
    assert crop is not None
    assert origin == (90, 40)
    assert crop.shape[:2] == (120, 120)


def test_crop_person_clamps_to_the_frame() -> None:
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    crop, origin = ppe.crop_person(image, (0.0, 0.0, 300.0, 200.0), 0.5)
    assert origin == (0, 0)
    assert crop.shape[:2] == (200, 300)


def test_crop_person_rejects_a_box_too_small_to_use() -> None:
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    crop, origin = ppe.crop_person(image, (10.0, 10.0, 12.0, 12.0), 0.0)
    assert crop is None
    assert origin == (0, 0)


# ── 2단 게이팅 (FR-3.1.1) ───────────────────────────────────────────


class FakeDetector:
    """주어진 목록을 그대로 돌려주는 검출기. 호출 입력을 기록한다."""

    def __init__(self, results: list[Detection]) -> None:
        self._results = results
        self.calls: list[tuple[int, int]] = []

    def open(self) -> None:
        return None

    def detect(self, image: np.ndarray) -> list[Detection]:
        self.calls.append(image.shape[:2])
        return list(self._results)


def test_ppe_model_does_not_run_without_a_person() -> None:
    """사람이 없으면 추론하지 않는다 — 배경 오검출을 원천 차단하는 구조다."""
    coco = FakeDetector([det("chair", (0, 0, 10, 10))])
    model = FakeDetector([det("no_helmet", (0, 0, 5, 5))])
    image = np.zeros((200, 300, 3), dtype=np.uint8)

    results = ppe.process(
        image, coco, model, person_label="person", pad=0.05, head_margin=8, use_clip=True
    )

    assert results == []
    assert model.calls == []


def test_ppe_model_receives_the_crop_not_the_full_frame() -> None:
    coco = FakeDetector([det("person", (100, 40, 180, 160))])
    model = FakeDetector([det("helmet", (5, 12, 20, 30)), det("vest", (5, 40, 30, 90))])
    image = np.zeros((200, 300, 3), dtype=np.uint8)

    results = ppe.process(
        image, coco, model, person_label="person", pad=0.0, head_margin=8, use_clip=True
    )

    assert len(results) == 1
    assert results[0][1].state == ppe.STATE_OK
    assert model.calls == [(120, 80)]


# ── 웹 중계 ────────────────────────────────────────────────────────


def test_relay_hands_out_the_latest_frame_with_a_sequence() -> None:
    relay = ppe.FrameRelay()
    assert relay.latest() == (None, 0)
    relay.publish(b"first")
    payload, seq = relay.latest()
    assert payload == b"first"
    assert seq == 1
    relay.publish(b"second")
    assert relay.latest() == (b"second", 2)


def test_web_page_is_served_on_the_chosen_port() -> None:
    relay = ppe.FrameRelay()
    server = ppe.start_web(relay, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            body = response.read().decode("utf-8")
        assert "/stream" in body
    finally:
        server.shutdown()
        server.server_close()


def test_web_controls_select_a_segment_reset_the_window_and_stop() -> None:
    relay = ppe.FrameRelay()
    stop_event = ppe.threading.Event()
    specs = [{"key": "all", "condition": "전신", "wear": "전부 착용", "expected": ppe.STATE_OK}]
    segments = ppe.SegmentLog(specs, 15, ["정면"])
    reset_calls = []
    server = ppe.start_web(
        relay,
        "127.0.0.1",
        0,
        segments,
        stop_event,
        lambda: reset_calls.append(True),
    )
    try:
        port = server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/segment?key=all", method="POST")
        urllib.request.urlopen(request, timeout=5).close()
        assert segments.status()["key"] == "all"
        assert reset_calls == [True]

        request = urllib.request.Request(f"http://127.0.0.1:{port}/stop", method="POST")
        urllib.request.urlopen(request, timeout=5).close()
        assert stop_event.is_set()
    finally:
        server.shutdown()
        server.server_close()


# ── 카메라 대기 ────────────────────────────────────────────────────


def config_with(ip: str) -> dict:
    return {"network": {"xiao_ip": ip, "vision_control_port": 80}}


def test_wait_for_camera_accepts_a_ready_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"ok": True, "sensor": "OV3660", "profile": "VGA"}).encode()

    class FakeResponse:
        def read(self) -> bytes:
            return payload

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse())
    assert ppe.wait_for_camera(config_with("10.0.0.5"), 0.05) is True


def test_wait_for_camera_rejects_a_board_without_a_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ `ok` 만 보면 안 된다 — 보드는 살아 있고 카메라만 죽은 상태가 있다."""
    payload = json.dumps({"ok": True, "sensor": "UNKNOWN"}).encode()

    class FakeResponse:
        def read(self) -> bytes:
            return payload

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse())
    monkeypatch.setattr(ppe.time, "sleep", lambda _s: None)
    assert ppe.wait_for_camera(config_with("10.0.0.5"), 0.01) is False


def test_wait_for_camera_survives_a_silent_board(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("연결 거부")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(ppe.time, "sleep", lambda _s: None)
    assert ppe.wait_for_camera(config_with("10.0.0.5"), 0.01) is False


# ── 보고서 ─────────────────────────────────────────────────────────


def report_args(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    base = {
        "xiao_ip": "10.0.0.5",
        "webcam": None,
        "images": None,
        "device": "mechdog-01",
        "scenario": "xiao",
        "window_ms": None,
        "hits": None,
        "no_clip_rule": False,
        "save_dir": str(tmp_path / "frames"),
        "report": str(tmp_path / "report.md"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def report_config() -> dict:
    return {
        "vision": {
            "providers": ["CPUExecutionProvider"],
            "ppe": {
                "model_path": "models/ppe.onnx",
                "input_size": 640,
                "conf_threshold": 0.5,
                "head_margin_px": 8,
            },
        }
    }


def test_report_records_the_alarm_history(tmp_path: Path) -> None:
    events = [
        {
            "t": 1.0,
            "tag": "00001",
            "people": 1,
            "states": [ppe.STATE_OK],
            "reasons": [],
            "labels": ["helmet", "vest"],
            "confirmed": False,
            "hits": 0,
        },
        {
            "t": 3.5,
            "tag": "00042",
            "people": 1,
            "states": [ppe.STATE_VIOLATION],
            "reasons": [],
            "labels": ["no_helmet"],
            "confirmed": True,
            "hits": 3,
        },
    ]
    path = tmp_path / "report.md"
    ppe.write_report(
        path,
        report_args(tmp_path),
        report_config(),
        1500,
        3,
        frames=2,
        elapsed=4.0,
        counts={ppe.STATE_OK: 1, ppe.STATE_VIOLATION: 1},
        reasons={},
        events=events,
    )

    text = path.read_text(encoding="utf-8")
    assert "## 알람 이력" in text
    assert "3.5초" in text
    assert "no_helmet" in text
    assert "1500ms 안 3회" in text


def test_report_says_so_when_no_violation_was_confirmed(tmp_path: Path) -> None:
    """확정이 없을 때 «위반이 없었다» 와 «창을 못 채웠다» 는 다른 사건이다."""
    path = tmp_path / "report.md"
    ppe.write_report(
        path,
        report_args(tmp_path, window_ms=20000),
        report_config(),
        20000,
        3,
        frames=0,
        elapsed=1.0,
        counts={},
        reasons={"머리 미검출": 2},
        events=[],
    )

    text = path.read_text(encoding="utf-8")
    assert "확정된 위반이 없다" in text
    assert "설정값 덮어씀" in text
    assert "머리 미검출 2건" in text


# ── 이미지 폴더 모드 종단 ───────────────────────────────────────────


def test_main_runs_offline_and_writes_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모델도 카메라도 없이 판정 경로 전체가 도는지 본다."""
    import cv2

    folder = tmp_path / "images"
    folder.mkdir()
    frame = np.zeros((200, 300, 3), dtype=np.uint8)
    cv2.imwrite(str(folder / "sample.jpg"), frame)

    def fake_detector(_config: object, *, section: str, **_kw: object) -> FakeDetector:
        if section == "coco":
            return FakeDetector([det("person", (100, 40, 180, 160))])
        return FakeDetector([det("no_helmet", (5, 5, 20, 20)), det("no_vest", (5, 40, 30, 90))])

    monkeypatch.setattr(ppe, "Detector", fake_detector)

    report = tmp_path / "out" / "report.md"
    session = tmp_path / "out" / "session.json"
    code = ppe.main(
        [
            "--images",
            str(folder),
            "--device",
            "mechdog-01",
            "--web-port",
            "0",
            "--hits",
            "1",
            "--save-dir",
            str(tmp_path / "frames"),
            "--report",
            str(report),
            "--session",
            str(session),
        ]
    )

    assert code == 0
    assert report.is_file()
    assert session.is_file()
    raw = json.loads(session.read_text(encoding="utf-8"))
    assert raw["source"] == f"이미지 폴더 {folder}"
    assert raw["device"] == "mechdog-01"
    assert raw["events"][0]["segment"] is None
    text = report.read_text(encoding="utf-8")
    assert ppe.STATE_VIOLATION in text
    assert (tmp_path / "frames" / "sample.jpg").is_file()
