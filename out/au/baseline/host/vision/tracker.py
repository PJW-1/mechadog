"""다중 인원 추적 (WBS 3.3.4 · FR-3.6 · ADR-27).

검출 목록에 프레임 간 지속되는 ID 를 붙인다 — 게이트가 «사람이 있는가» 라면 이쪽은
«누구인가» 다. 신경망이 아니라 이미 나온 박스를 겹침(IoU)으로 잇는 후처리다 (FR-3.6.1).

- ByteTrack 2단계 결합·칼만 예측은 쓰지 않는다 (ADR-27 ①②).
- 소실 버퍼는 프레임 수가 아니라 시간(`track_lost_ms`)이다 (ADR-27 ③).
- ⚠️ ID 는 재사용하지 않는다(단조 증가) — 만료된 ID 에 걸린 인증 세션을 다음 사람이 물려받지 않게.
- 시각은 `now_ms` 로 받는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from host.common.logging_setup import event_logger
from host.vision.detector import Detection

LOG = event_logger("mechadog.vision")

Box = tuple[float, float, float, float]


def iou(a: Box, b: Box) -> float:
    """두 박스의 교집합 / 합집합. 겹치지 않으면 0.

    몇 개짜리 목록이라 넘파이 없이 계산한다.
    """
    inter_w = min(a[2], b[2]) - max(a[0], b[0])
    inter_h = min(a[3], b[3]) - max(a[1], b[1])
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = inter_w * inter_h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    # 면적 0 인 박스가 섞이면 0 나눗셈이 된다 — 그런 박스는 겹치지 않은 것으로 본다.
    return inter / union if union > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Track:
    """한 사람의 추적 상태."""

    track_id: int
    box: Box
    score: float
    #: 마지막으로 이 대상을 검출한 시각. 소실 판정의 기준이다.
    last_seen_ms: int

    @property
    def height(self) -> float:
        """박스 높이 — 거리의 대리값이며 주 대상 선정과 상한의 단일 기준이다 (FR-3.8.2 · FR-3.8.5)."""
        return self.box[3] - self.box[1]


class PersonTracker:
    """`person` 검출에 지속 ID 를 붙인다 — 가장 많이 겹치는 짝부터 확정하는 탐욕 결합 (ADR-27 ④).

    추론 스레드 하나에서만 부른다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        vision = config["vision"]
        spec = vision["tracker"]
        self._label = str(vision["coco"]["person_class"])
        self._iou = float(spec["iou_match_threshold"])
        self._lost_ms = int(spec["track_lost_ms"])
        self._max = int(vision["max_tracked_persons"])
        self._tracks: dict[int, Track] = {}
        #: 다음에 내줄 ID. 줄어들지 않는다.
        self._next_id = 1

    @property
    def tracked(self) -> int:
        """소실 버퍼까지 포함해 지금 쥐고 있는 대상 수. 상한 검증에 쓴다."""
        return len(self._tracks)

    def update(self, detections: Sequence[Detection], now_ms: int) -> tuple[Track, ...]:
        """검출 한 묶음을 넣고 이번 프레임에 보인 대상들만 돌려준다(소실 버퍼의 대상은 빼고)."""
        people = [d for d in detections if d.label == self._label]
        # 결합보다 만료가 먼저다 — 죽었어야 할 대상이 새 검출을 가로채지 않게.
        self._expire(now_ms)
        matched = self._associate(people)
        seen: list[Track] = []
        for index, detection in enumerate(people):
            track_id = matched.get(index)
            if track_id is None:
                track = self._open(detection, now_ms)
            else:
                track = replace(
                    self._tracks[track_id],
                    box=detection.box,
                    score=detection.score,
                    last_seen_ms=now_ms,
                )
            self._tracks[track.track_id] = track
            seen.append(track)
        self._enforce_cap(now_ms)
        # 상한에 걸려 버려진 대상은 결과에서도 뺀다.
        return tuple(t for t in seen if t.track_id in self._tracks)

    # ── 내부 ────────────────────────────────────────────────
    def _associate(self, people: Sequence[Detection]) -> dict[int, int]:
        """검출 index → `track_id`. 겹침이 임계 미만인 짝은 잇지 않는다."""
        if not people or not self._tracks:
            return {}
        candidates = sorted(
            (
                (iou(track.box, detection.box), track.track_id, index)
                for track in self._tracks.values()
                for index, detection in enumerate(people)
            ),
            reverse=True,
        )
        matched: dict[int, int] = {}
        used: set[int] = set()
        for overlap, track_id, index in candidates:
            if overlap < self._iou:
                break  # 정렬돼 있으므로 여기부터는 전부 미달이다
            if track_id in used or index in matched:
                continue
            matched[index] = track_id
            used.add(track_id)
        return matched

    def _open(self, detection: Detection, now_ms: int) -> Track:
        track = Track(
            track_id=self._next_id,
            box=detection.box,
            score=detection.score,
            last_seen_ms=now_ms,
        )
        self._next_id += 1
        LOG.info("track_opened", track_id=track.track_id, score=round(detection.score, 3))
        return track

    def _expire(self, now_ms: int) -> None:
        for track in list(self._tracks.values()):
            if now_ms - track.last_seen_ms >= self._lost_ms:
                self._drop(track, "lost", now_ms)

    def _enforce_cap(self, now_ms: int) -> None:
        """동시 추적 인원을 `max_tracked_persons` 로 제한한다 (FR-3.8.5).

        지금 보이는 대상이 소실 버퍼의 대상보다 앞서고, 그 안에서는 박스 높이 순이다.
        """
        if len(self._tracks) <= self._max:
            return
        ranked = sorted(
            self._tracks.values(),
            key=lambda t: (t.last_seen_ms == now_ms, t.height),
            reverse=True,
        )
        for track in ranked[self._max :]:
            self._drop(track, "over_cap", now_ms)
        LOG.warning("tracker_over_capacity", kept=self._max, dropped=len(ranked) - self._max)

    def _drop(self, track: Track, reason: str, now_ms: int) -> None:
        del self._tracks[track.track_id]
        # 버린 ID 를 로그에 남긴다 — 그 ID 의 인증 세션도 만료된다 (FR-3.6.3).
        LOG.info(
            "track_dropped",
            track_id=track.track_id,
            reason=reason,
            age_ms=now_ms - track.last_seen_ms,
        )
