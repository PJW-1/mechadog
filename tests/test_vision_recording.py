"""비전 결과 기록 단독 검증 — `host.telemetry.vision_recording.VisionRecording` (Runtime 분할 14단계).

실기 동시 기록·PPE 시험이 운용 루프와 맞물리는 시나리오는 `test_session_recording.py`·
`test_ppe_test_mode.py` 가 `Runtime` 으로 본다. 여기서는 런타임 없이 프레임 저장 간격,
PPE 시험의 설정 거부, 워커 싱크가 붙으면 루프가 같은 결과를 두 번 기록하지 않는 것을 본다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from host.common.config import ConfigError
from host.telemetry.vision_recording import VisionRecording
from host.vision.person import FallenVerdict, Sighting
from host.vision.worker import VisionResult


class Recorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.frames: list[tuple[bytes, int, int]] = []

    def record(self, kind: str, **fields: Any) -> None:
        self.records.append((kind, fields))

    def save_frame(self, jpeg: bytes, *, frame_seq: int, received_ms: int) -> str:
        self.frames.append((jpeg, frame_seq, received_ms))
        return f"frames/{frame_seq}.jpg"


def _result(seq: int) -> VisionResult:
    return VisionResult(
        detections=(),
        jpeg=b"jpeg",
        frame_seq=seq,
        frame_width=640,
        frame_height=480,
        frame_received_ms=seq * 100,
        completed_ms=seq * 100,
        inference_ms=1,
        sighting=Sighting(False, False, 0, 0, None, None),
        tracks=(),
        markers=(),
        fallen=FallenVerdict(False, False, False, 0.5, 0),
    )


def _recording(
    recorder: Recorder | None, *, ppe_test: bool = False, motion_lock: bool = True
) -> VisionRecording:
    return VisionRecording(
        {"vision": {"ppe": {"test_mode": ppe_test}}},
        mission=SimpleNamespace(enables=lambda _what: True),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        record_frame_ms=1000,
        motion_lock=motion_lock,
    )


def test_frames_are_saved_only_at_the_record_interval() -> None:
    recorder = Recorder()
    recording = _recording(recorder)
    recording.take(_result(1), 0)
    recording.take(_result(2), 500)
    recording.take(_result(3), 1000)
    assert recorder.frames == [(b"jpeg", 1, 100), (b"jpeg", 3, 300)]
    assert [kind for kind, _ in recorder.records] == ["vision"] * 3
    assert [fields["frame_file"] for _, fields in recorder.records] == [
        "frames/1.jpg",
        None,
        "frames/3.jpg",
    ]


def test_ppe_test_needs_the_motion_lock() -> None:
    with pytest.raises(ConfigError):
        _recording(None, ppe_test=True, motion_lock=False)


def test_ppe_test_records_in_the_loop_until_the_worker_sink_is_attached() -> None:
    recorder = Recorder()
    recording = _recording(recorder, ppe_test=True)
    recording.take(_result(1), 100)
    assert [kind for kind, _ in recorder.records] == ["ppe_debug", "vision"]
    sinks: list[Any] = []
    recording.attach(SimpleNamespace(set_result_sink=sinks.append))  # type: ignore[arg-type]
    assert sinks == [recording.record_ppe_frame]
    recorder.records.clear()
    recording.take(_result(2), 200)
    assert [kind for kind, _ in recorder.records] == ["vision"], "워커가 이미 기록했다"


def test_without_a_recorder_or_ppe_test_nothing_is_recorded() -> None:
    recording = _recording(None)
    recording.attach(SimpleNamespace())  # type: ignore[arg-type]
    recording.take(_result(1), 0)
    assert not recording.ppe_test
