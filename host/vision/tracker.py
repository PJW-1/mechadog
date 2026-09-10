"""다중 인원 추적 (WBS 3.3.4 · FR-3.6).

검출 목록에 **프레임 간 지속되는 ID** 를 붙인다. 게이트(`3.3.3`)가 *"사람이
있는가"* 에 답한다면 이쪽은 *"누구인가"* 에 답한다 — 인증이 ID 에 귀속되기
때문이다(FR-3.6.2).

⚠️ **신경망이 아니다** (FR-3.6.1). 추가 추론 패스가 없고 모델도 세션도 쥐지
않는다. 이미 나온 박스들을 겹침으로 잇는 후처리일 뿐이다.

⚠️ **ByteTrack 의 2단계 결합은 넣지 않았다.** 그 알고리즘의 구별점은 *임계
미달의 낮은 점수 검출*로 가려진 대상을 되살리는 것인데, **우리 검출기는 임계
미달을 반환하지 않고 버린다**(`Detector._finalize`). 쓰려면 검출 임계를 내려
실기로 검증한 경로를 흔들어야 하고, 그 대가로 얻는 것은 대체로 *박스의 연속성*
이며 **ID 의 연속성은 아래 소실 버퍼가 이미 지킨다.** 근거와 재검토 조건은
[ADR-27] 에 적었다.

⚠️ **칼만 필터로 위치를 예측하지 않는다.** 예측의 목적은 긴 공백을 건너 같은
ID 를 이어 붙이는 것인데, **인증 세션이 ID 에 귀속되므로**(FR-3.6.2) ID 를
억지로 잇는 것은 *인증을 억지로 잇는 것*이다. ID 재발급은 재인증을 뜻하고
그것이 안전측이다(FR-3.6.3). 짧은 공백은 예측 없이도 겹침으로 이어진다 —
25fps 에서 120ms 공백은 사람이 18cm 움직인 정도다.

⚠️ **소실 버퍼는 프레임 수가 아니라 시간이다.** 원본 ByteTrack 은
`track_buffer=30 프레임` 으로 정의하는데, 그러면 **의미가 추론률에 종속된다** —
같은 30프레임이 10fps 에서 3초, 25fps 에서 1.2초다. 결정 22·25번과 같은 형태이며
같은 실수를 세 번 하지 않기 위해 `track_lost_ms` 로 둔다.

⚠️ **ID 는 재사용하지 않는다.** 만료된 ID 를 다시 내주면 그 ID 에 걸려 있던
인증 세션을 **다음 사람이 물려받는다.** 단조 증가 카운터인 것이 그래서다.

⚠️ **이 모듈은 시간을 만들지 않는다.** `now_ms` 를 받으므로 가상 시간으로 전수
검증된다.
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

    `detector.nms` 와 같은 계산이지만 **넘파이를 쓰지 않는다** — 여기서 다루는
    것은 최대 몇 개짜리 목록이고, 배열로 만들면 축과 모양을 틀릴 여지만 생긴다.
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
    """한 사람의 추적 상태. **ID 가 이 자료의 존재 이유다.**"""

    track_id: int
    box: Box
    score: float
    #: 마지막으로 이 대상을 검출한 시각. 소실 판정의 기준이다.
    last_seen_ms: int

    @property
    def height(self) -> float:
        """박스 높이. **거리의 대리값이다** — 주 대상 선정과 상한이 쓰는 단일 기준
        (FR-3.8.2 · FR-3.8.5). 폭이 아니라 높이인 이유는 사람이 서 있고 좌우로는
        팔·소지품에 따라 흔들리기 때문이다."""
        return self.box[3] - self.box[1]


class PersonTracker:
    """`person` 검출에 지속 ID 를 붙인다. **탐욕적 겹침 결합이다.**

    헝가리안(최적 할당) 대신 *가장 많이 겹치는 짝부터 확정*하는 방식을 쓴다.
    동시 추적 상한이 5명(FR-3.8.5)이라 최적 할당과 결과가 갈리는 경우가 드물고,
    갈리는 상황은 **두 사람의 박스가 서로 비슷하게 겹칠 때**인데 그때는 어느 쪽을
    골라도 옳다고 말할 수 없다. 의존성 하나(`scipy`)를 그것 때문에 들이지 않는다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        vision = config["vision"]
        spec = vision["tracker"]
        self._label = str(vision["coco"]["person_class"])
        self._iou = float(spec["iou_match_threshold"])
        self._lost_ms = int(spec["track_lost_ms"])
        self._max = int(vision["max_tracked_persons"])
        self._tracks: dict[int, Track] = {}
        #: 다음에 내줄 ID. **줄어들지 않는다** (모듈 주석의 재사용 금지).
        self._next_id = 1

    @property
    def tracked(self) -> int:
        """소실 버퍼까지 포함해 지금 쥐고 있는 대상 수. 상한 검증에 쓴다."""
        return len(self._tracks)

    def update(self, detections: Sequence[Detection], now_ms: int) -> tuple[Track, ...]:
        """검출 한 묶음을 넣고 **이번 프레임에 보인 대상들**을 돌려준다.

        소실 버퍼에 있는(이번에 안 보인) 대상은 돌려주지 않는다. *"지금 누가
        앞에 있나"* 와 *"방금까지 누가 있었나"* 는 다른 질문이고, 섞으면 대시보드에
        없는 사람이 그려진다.
        """
        people = [d for d in detections if d.label == self._label]
        # ⚠️ **결합보다 만료가 먼저다.** 순서를 바꾸면 이미 죽었어야 할 대상이
        # 새 검출을 가로채 ID 가 잘못 이어진다.
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
        # 상한에 걸려 버려진 대상은 결과에서도 빠진다 — 추적하지 않는 대상을
        # 추적 결과로 내보내면 소비자가 죽은 ID 를 들고 있게 된다.
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

        ⚠️ **보이는 대상이 소실 버퍼의 대상보다 앞선다.** 크기만으로 정렬하면
        *방금 사라진 큰 사람*을 들고 *지금 앞에 있는 작은 사람*을 버릴 수 있다.
        그 뒤는 PRD 의 단일 기준(박스 높이 = 거리)을 따른다.
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
        # ⚠️ **버렸다는 사실을 남긴다.** ID 가 사라지면 그 ID 의 인증 세션도
        # 만료돼야 하므로(FR-3.6.3), 나중에 인증이 붙을 때 이 지점이 근거가 된다.
        LOG.info(
            "track_dropped",
            track_id=track.track_id,
            reason=reason,
            age_ms=now_ms - track.last_seen_ms,
        )
