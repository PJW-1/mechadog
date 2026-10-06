"""화기 위험물(라이터·보조배터리) 검출 — 위험구역 방문 중에만 돈다 (ADR-43 대안 ⓐ 개정).

`models/hazard.onnx`(YOLOX-S 2클래스)로 프레임 전체를 본다. 확정은 PPE 위반과 같은 모양이다
(`PpeDetector`) — 금지 대상(`vision.hazard.alarm_classes`)이 `confirm_window_ms` 안에서
`hits_required` 번 이상 보여야 그 물건을 확정한다. 한 프레임의 오검출은 경고가 되지 않는다.

**어느 구역에서 켜는지는 여기서 정하지 않는다.** 워커는 켜져 있을 때만 이것을 부르고, 켜고
끄는 것은 런타임이 구역 점검기(`ZoneInspector.watching_hazards`)를 보고 한다.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from host.vision.detector import Detection

#: 모델 출력 순서 — **모델과의 계약이다.** 바꾸면 라이터를 보조배터리로 부른다.
HAZARD_CLASSES = ("lighter", "powerbank")


@dataclass(frozen=True, slots=True)
class HazardVerdict:
    """한 프레임의 위험물 판정."""

    #: 이 프레임에서 보인 금지 대상 (임계값 이상 · 금지 대상만).
    detections: tuple[Detection, ...] = ()
    #: 창 안에서 `hits_required` 번 이상 보여 확정한 물건 이름 (모델 순서).
    confirmed: tuple[str, ...] = ()


class HazardDetector:
    """금지 대상의 검출을 세어 확정한다. 추론 스레드에서만 부른다."""

    def __init__(self, config: Mapping[str, Any], detector: Any) -> None:
        spec = config["vision"]["hazard"]
        self._detector = detector
        alarm = tuple(str(label) for label in spec["alarm_classes"])
        #: 모델 순서를 지킨 금지 대상 — 기록·문장이 매번 같은 순서로 나온다.
        self._alarm = tuple(label for label in HAZARD_CLASSES if label in alarm)
        self._window_ms = int(spec["confirm_window_ms"])
        self._hits_required = int(spec["hits_required"])
        #: 긴 변이 이보다 작은 박스는 버린다(0 이면 끔). 2026-10-06 합성 학습 모델이 PC 본체의
        #: 작은 빨간 표시(16×10px)를 보조배터리로 잡았다 — 실물 라이터는 25~50px.
        self._min_side_px = float(spec.get("min_box_side_px", 0) or 0)
        self._hits: dict[str, deque[int]] = {label: deque() for label in self._alarm}

    @property
    def detector(self) -> Any:
        """감싼 검출기 — 모델 메타데이터(`model_summary`)를 묻는 데 쓴다."""
        return self._detector

    def open(self) -> None:
        self._detector.open()

    def reset(self) -> None:
        """창을 비운다 — 꺼질 때. 다른 구역에서 센 것이 다음 방문의 확정을 앞당기지 않게."""
        for hits in self._hits.values():
            hits.clear()

    def observe(self, image: np.ndarray, now_ms: int) -> HazardVerdict:
        found = tuple(
            d
            for d in self._detector.detect(image)
            if d.label in self._hits
            and max(d.box[2] - d.box[0], d.box[3] - d.box[1]) >= self._min_side_px
        )
        seen = {d.label for d in found}
        for label, hits in self._hits.items():
            if label in seen:
                hits.append(now_ms)
            while hits and now_ms - hits[0] > self._window_ms:
                hits.popleft()
        confirmed = tuple(
            label for label in self._alarm if len(self._hits[label]) >= self._hits_required
        )
        return HazardVerdict(detections=found, confirmed=confirmed)
