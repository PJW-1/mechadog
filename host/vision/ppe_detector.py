"""Factory-only PPE judgement for the primary tracked person (WBS 3.7.3)."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from host.vision.detector import Detection
from host.vision.tracker import Track

PPE_CLASSES = ("helmet", "no_helmet", "vest", "no_vest")
OK = "적합"
VIOLATION = "위반"
UNDETERMINED = "확인불가"


@dataclass(frozen=True, slots=True)
class PpeVerdict:
    track_id: int
    state: str
    reason: str = ""
    confirmed: bool = False
    clipped: bool = False
    required: tuple[str, ...] = ("helmet", "vest")


class PpeDetector:
    """Infer only on a person crop; retain the violation window per track."""

    def __init__(self, config: Mapping[str, Any], detector: Any) -> None:
        spec = config["vision"]["ppe"]
        self._detector = detector
        self._margin = float(spec["head_margin_px"])
        self._pad = float(spec["crop_pad"])
        self._clip = bool(spec["require_head_visible"])
        self._window_ms = int(spec["violation_window_ms"])
        self._hits_required = int(spec["violation_hits_required"])
        self._hits: dict[int, deque[int]] = {}
        self._required: tuple[str, ...] = ("helmet", "vest")

    def set_requirements(self, required: tuple[str, ...]) -> None:
        """워커 스레드에서만 호출. 구역이 바뀌면 이전 요구 항목의 위반 창을 버린다."""
        if required != self._required:
            self._required = required
            self.reset()

    def reset(self) -> None:
        self._hits.clear()

    @property
    def detector(self) -> Any:
        """감싼 검출기 — 모델 메타데이터(`model_summary`)를 묻는 데 쓴다."""
        return self._detector

    def open(self) -> None:
        self._detector.open()

    def observe(self, image: np.ndarray, tracks: Sequence[Track], now_ms: int) -> PpeVerdict | None:
        if not tracks:
            self.reset()
            return None
        track = max(tracks, key=lambda t: t.height)
        required = self._required
        if not required:
            return PpeVerdict(track.track_id, OK, "이 구역은 PPE 검사 미지정", required=required)
        self._hits = {track.track_id: self._hits.get(track.track_id, deque())}
        if "helmet" in required and self._clip and track.box[1] <= self._margin:
            return PpeVerdict(
                track.track_id, UNDETERMINED, "머리 클리핑", clipped=True, required=required
            )

        height, width = image.shape[:2]
        x1, y1, x2, y2 = track.box
        dx, dy = (x2 - x1) * self._pad, (y2 - y1) * self._pad
        left, top = max(0, int(x1 - dx)), max(0, int(y1 - dy))
        right, bottom = min(width, int(x2 + dx)), min(height, int(y2 + dy))
        if right - left < 8 or bottom - top < 8:
            return PpeVerdict(track.track_id, UNDETERMINED, "크롭 실패", required=required)
        found: list[Detection] = self._detector.detect(image[top:bottom, left:right])
        heads = [d for d in found if d.label in ("helmet", "no_helmet")]
        torsos = [d for d in found if d.label in ("vest", "no_vest")]
        if ("helmet" in required and not heads) or ("vest" in required and not torsos):
            reason = "머리 미검출" if "helmet" in required and not heads else "몸통 미검출"
            return PpeVerdict(track.track_id, UNDETERMINED, reason, required=required)
        if (
            "helmet" in required
            and self._clip
            and min(d.box[1] for d in heads) + top <= self._margin
        ):
            return PpeVerdict(
                track.track_id, UNDETERMINED, "머리 클리핑", clipped=True, required=required
            )
        if not any(d.label in tuple("no_" + item for item in required) for d in found):
            self._hits[track.track_id].clear()
            return PpeVerdict(track.track_id, OK, required=required)

        hits = self._hits[track.track_id]
        hits.append(now_ms)
        while hits and now_ms - hits[0] > self._window_ms:
            hits.popleft()
        return PpeVerdict(
            track.track_id, VIOLATION, confirmed=len(hits) >= self._hits_required, required=required
        )
