"""비전 결과 기록 — 실기 동시 기록(`--record-dir`)의 `vision` 줄과 PPE 시험(`--ppe-test`)의 `ppe_debug`.

판단은 아무것도 바꾸지 않는다. `take` 는 운용 루프 스레드의 `Runtime._poll_vision` 이 새
추론 결과마다 부르고, PPE 시험의 결과 싱크(`attach`)는 비전 워커 스레드에서 불린다.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from host.common.config import ConfigError
from host.common.logging_setup import event_logger
from host.vision.ppe_detector import ppe_payload

if TYPE_CHECKING:
    from collections.abc import Mapping

    from host.behavior.mission import Mission
    from host.telemetry.session_recorder import SessionRecorder
    from host.vision.worker import VisionResult, VisionSource

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")


class VisionRecording:
    """새 추론 결과를 세션 기록에 남기고, PPE 시험이면 판정 대신 시험 결과만 기록한다."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        mission: Mission,
        recorder: SessionRecorder | None,
        record_frame_ms: int,
        motion_lock: bool,
    ) -> None:
        self._mission = mission
        self._recorder = recorder
        self._record_frame_ms = record_frame_ms
        self._last_frame_saved_ms: int | None = None
        #: PPE 시험 (`vision.ppe.test_mode`) — 런타임은 이때 보호구 판정을 돌리지 않는다.
        self.ppe_test = bool(config["vision"]["ppe"].get("test_mode", False))
        if self.ppe_test and (not motion_lock or not mission.enables("ppe")):
            raise ConfigError("PPE 시험은 공장 모드와 보행 잠금이 필요하다")
        self._records_in_worker = False

    def attach(self, vision: VisionSource) -> None:
        """PPE 시험이면 워커가 추론마다 직접 기록하게 결과 싱크를 붙인다 (`Runtime.begin`)."""
        if self.ppe_test and hasattr(vision, "set_result_sink"):
            vision.set_result_sink(self.record_ppe_frame)
            self._records_in_worker = True

    def take(self, result: VisionResult, now_ms: int) -> None:
        """루프가 새로 본 추론 결과 하나를 기록한다."""
        if self.ppe_test and not self._records_in_worker:
            self.record_ppe_frame(result)
        if self._recorder is not None:
            self.record_vision(result, now_ms)

    def record_vision(self, result: VisionResult, now_ms: int) -> None:
        """새 추론 결과 하나 — 모델 출력 그대로. 프레임은 `record_frame_ms` 간격으로만 파일로 둔다."""
        assert self._recorder is not None
        frame_file = None
        due = self._last_frame_saved_ms is None or (
            now_ms - self._last_frame_saved_ms >= self._record_frame_ms
        )
        if due and result.jpeg:
            frame_file = self._recorder.save_frame(
                result.jpeg, frame_seq=result.frame_seq, received_ms=result.frame_received_ms
            )
            self._last_frame_saved_ms = now_ms
        self._recorder.record(
            "vision",
            at_ms=now_ms,
            frame_seq=result.frame_seq,
            frame_received_ms=result.frame_received_ms,
            completed_ms=result.completed_ms,
            inference_ms=round(result.inference_ms, 2),
            size=[result.frame_width, result.frame_height],
            detections=[
                {"label": d.label, "score": round(d.score, 3), "box": [round(v, 1) for v in d.box]}
                for d in result.detections
            ],
            person_present=result.sighting.present,
            ppe=ppe_payload(result),
            frame_file=frame_file,
            # 재생(`tools/ops/replay_session.py`)이 같은 결과를 다시 만들 수 있게 판정 전체를 남긴다.
            sighting=asdict(result.sighting),
            tracks=[asdict(track) for track in result.tracks],
            fallen=asdict(result.fallen),
            markers=[asdict(marker) for marker in result.markers],
            # `ppe` 는 현장 분석 도구가 읽는 트랙별 목록이다 — 재생용 대표 판정은 따로 둔다.
            ppe_verdict=None if result.ppe is None else asdict(result.ppe),
            hazard=None if result.hazard is None else asdict(result.hazard),
        )

    def record_ppe_frame(self, result: VisionResult) -> None:
        """시험 결과만 기록한다. FSM·경보·자세 명령은 호출하지 않는다."""
        payload = ppe_payload(result)
        if not self._mission.enables("ppe"):
            return
        LOG.info("ppe_debug", frame_seq=result.frame_seq, ppe=payload)
        if self._recorder is not None:
            self._recorder.record(
                "ppe_debug",
                at_ms=result.completed_ms,
                frame_seq=result.frame_seq,
                ppe=payload,
                person_down_reference=[
                    {"score": d.score, "box": list(d.box)} for d in result.person_down_reference
                ],
                reference_reason=result.person_down_reference_reason,
            )
