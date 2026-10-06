"""Factory-only PPE judgement for the primary tracked person (WBS 3.7.3)."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
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
    regions: tuple[PpeRegion, ...] = ()


@dataclass(frozen=True, slots=True)
class PpeRegion:
    item: str
    label: str
    box: tuple[float, float, float, float]
    score: float | None = None
    reason: str = ""


def ppe_payload(result: Any) -> list[dict[str, Any]]:
    """WS와 기록이 같은 원본 좌표·판정 근거를 사용한다."""
    verdicts = getattr(result, "ppe_tracks", ())
    single = getattr(result, "ppe", None)
    if not verdicts and single is not None:
        verdicts = (single,)
    return [asdict(verdict) for verdict in verdicts]


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
        self._regions: tuple[PpeRegion, ...] = ()

    def observe_all(
        self, image: np.ndarray, tracks: Sequence[Track], now_ms: int
    ) -> tuple[PpeVerdict, ...]:
        active = {track.track_id for track in tracks}
        self._hits = {key: hits for key, hits in self._hits.items() if key in active}
        verdicts = []
        for track in tracks:
            x1, y1, x2, y2 = track.box
            h = y2 - y1
            self._regions = tuple(
                PpeRegion(item, "UNDETERMINED", box, reason="미검출")
                for item, box in (
                    ("helmet", (x1, y1, x2, y1 + h * 0.3)),
                    ("vest", (x1, y1 + h * 0.3, x2, y1 + h * 0.75)),
                )
            )
            verdict = self._observe_one(image, (track,), now_ms)
            assert verdict is not None
            regions = self._regions
            if verdict.clipped or verdict.reason == "크롭 실패":
                regions = tuple(
                    replace(r, label="UNDETERMINED", reason=verdict.reason) for r in regions
                )
            verdicts.append(replace(verdict, regions=regions))
        return tuple(verdicts)

    def set_requirements(self, required: tuple[str, ...]) -> None:
        """워커 스레드에서만 호출. 구역이 바뀌면 이전 요구 항목의 위반 창을 버린다."""
        if required != self._required:
            self._required = required
            self.reset()

    def reset(self) -> None:
        self._hits.clear()

    def open(self) -> None:
        self._detector.open()

    def observe(self, image: np.ndarray, tracks: Sequence[Track], now_ms: int) -> PpeVerdict | None:
        if not tracks:
            self.reset()
            return None
        primary = max(tracks, key=lambda track: track.height)
        return self.observe_all(image, (primary,), now_ms)[0]

    def _observe_one(
        self, image: np.ndarray, tracks: Sequence[Track], now_ms: int
    ) -> PpeVerdict | None:
        if not tracks:
            self.reset()
            return None
        track = max(tracks, key=lambda t: t.height)
        required = self._required
        self._hits.setdefault(track.track_id, deque())
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
        regions = []
        for region in self._regions:
            choices = [d for d in found if d.label in (region.item, "no_" + region.item)]
            if choices:
                # 위반을 적합으로 덮지 않는다. 사건 판정의 기존 보수적 규칙과 같다.
                missing = [d for d in choices if d.label.startswith("no_")]
                best = max(missing or choices, key=lambda d: d.score)
                bx1, by1, bx2, by2 = best.box
                region = PpeRegion(
                    region.item,
                    best.label,
                    (bx1 + left, by1 + top, bx2 + left, by2 + top),
                    best.score,
                )
            if (
                region.item == "helmet"
                and self._clip
                and (track.box[1] <= self._margin or region.box[1] <= self._margin)
            ):
                region = replace(region, label="UNDETERMINED", reason="머리 클리핑")
            regions.append(region)
        self._regions = tuple(regions)
        if not required:
            # 착용 상태는 모든 구역에서 추론·표시한다. 필수 항목이 없으면 경고는 없다.
            self._hits[track.track_id].clear()
            return PpeVerdict(
                track.track_id, OK, "필수 보호구 없음 — 착용 여부 표시", required=required
            )
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
