"""사원증 ArUco 인증 (WBS 3.8.1 · FR-10.1 · ADR-28).

픽셀에서 마커를 찾는 것은 `vision/badge.py`(cv2 를 쓰는 유일한 곳)이고, 이 파일은 누구의
인증인지 정하고 세션을 들고 있다 — 픽셀을 모른다.

- 귀속 단위는 `auth.bind_to_track_id` 가 정한다. 켜면 세션이 추적 ID 에 붙고(FR-3.6.2) ID 가
  사라지면 만료된다(FR-3.6.3). 끄면(운용 설정) 세션 하나(키 0)가 현장 전체에 붙는다 (ADR-27 «운용 설정»).
- 추적 ID 귀속에서 마커 주인은 마커 중심을 품은 추적 박스이고, 둘 이상이면 큰(가까운) 쪽이다
  (FR-3.8.2). 추적이 한 명뿐이면 박스 밖 마커도 그 사람에게 붙인다 — 바짝 붙으면 `person`
  검출이 무너진다. 주인이 없는 미등록 마커는 무시한다.
- 시도 횟수는 본 마커 ID 의 집합 크기다 — 같은 사원증을 계속 봐도 한 번이다 (ADR-28 ⑤).
- 미등록 마커는 1초 창 안에 `unknown_marker_min_frames` 회 읽혀야 시도로 센다 (ADR-28 ⑤).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from host.common.logging_setup import event_logger
from host.vision.badge import Marker
from host.vision.tracker import Track

LOG = event_logger("mechadog.auth")


class Outcome(Enum):
    """관측 한 번의 결과. 호출부가 사건으로 옮긴다."""

    NOTHING = "nothing"  # 볼 것이 없었다
    GRANTED = "granted"  # 승인 — `AUTH_OK`
    BADGE_SEEN = "badge_seen"  # 등록 사원증을 읽었으나 귀속할 추적이 없다 — `AUTH_OK`
    REJECTED = "rejected"  # 등록되지 않은 사원증. 기회가 남았다
    EXHAUSTED = "exhausted"  # 시도 횟수 초과 — `AUTH_FAILED` (FR-10.3)


@dataclass(slots=True)
class Session:
    """한 사람(또는 현장)의 인증 상태."""

    holder: str | None = None
    granted_ms: int | None = None
    #: 이미 판정한 마커 ID 들 — 크기가 시도 횟수다.
    seen: set[int] = field(default_factory=set)

    @property
    def attempts(self) -> int:
        return len(self.seen)


class Authenticator:
    """마커를 사람에게 귀속시키고 세션을 들고 있다. 시각은 `now_ms` 로 받는다."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        auth = config["auth"]
        # YAML 키가 `0:` 이든 `"0":` 이든 같게 읽는다.
        self._badges = {
            int(key): str(value) for key, value in (auth["badge_marker_map"] or {}).items()
        }
        self._valid_ms = int(auth["session_valid_s"]) * 1000
        self._max_attempts = int(auth["max_attempts"])
        self._bind_to_track = bool(auth["bind_to_track_id"])
        self._sessions: dict[int, Session] = {}
        #: 추적 없이 읽힌 등록 사원증의 보류 승인 — 다음에 나타난 추적에 붙는다.
        self._pending: tuple[str, int] | None = None
        #: 미등록 마커의 안정성 게이트(프레임 수).
        self._min_unknown_frames = int(auth.get("unknown_marker_min_frames", 3))
        #: 미등록 마커 후보 — ID -> (창 안에서 읽힌 횟수, 마지막으로 읽힌 시각).
        self._unproven: dict[int, tuple[int, int]] = {}

    # ── 상태 ────────────────────────────────────────────────
    def holder(self, track_id: int, now_ms: int) -> str | None:
        """그 대상의 인증된 신분. 유효하지 않으면 `None`."""
        session = self._sessions.get(track_id if self._bind_to_track else 0)
        if session is None or session.granted_ms is None:
            return None
        if now_ms - session.granted_ms >= self._valid_ms:
            return None  # FR-10.2.4 — 유효 시간이 지났다
        return session.holder

    def attempts(self, track_id: int) -> int:
        session = self._sessions.get(track_id if self._bind_to_track else 0)
        return 0 if session is None else session.attempts

    def reset(self) -> None:
        """새 인증 창에서는 이전 사람의 사원증을 다시 사용하지 않는다."""
        self._sessions.clear()
        self._pending = None
        self._unproven.clear()

    def all_authenticated(self, tracks: Sequence[Track], now_ms: int) -> bool:
        """보이는 전원이 인증됐나 — 한 명이라도 미인증이면 `False` (FR-3.8.1 · ADR-28 ④).

        현장 인증(`bind_to_track_id: false`)이면 현장 세션이 유효한지만 본다.
        """
        if not self._bind_to_track:
            return self.holder(0, now_ms) is not None  # 현장 인증은 보이는 ID 와 무관하다.
        if not tracks:
            return False
        return all(self.holder(track.track_id, now_ms) is not None for track in tracks)

    # ── 관측 ────────────────────────────────────────────────
    def note_tracks(self, tracks: Sequence[Track]) -> None:
        """지금 보이는 대상들을 알린다. 추적 ID 귀속이면 사라진 ID 의 세션을 만료시킨다 (FR-3.6.3).

        추적 없이 읽혀 보류된 사원증은 다음에 나타난 가장 큰 대상에게 붙인다.
        """
        if not self._bind_to_track:
            return  # 추적 ID 변동으로 현장 인증을 취소하지 않는다.
        alive = {track.track_id for track in tracks}
        for track_id in list(self._sessions):
            if track_id not in alive:
                del self._sessions[track_id]
                LOG.info("auth_session_dropped", track_id=track_id, reason="track_lost")
        # 사람 없이 읽힌 사원증은 다음에 나타난 가장 가까운 대상에게 붙인다.
        if self._pending is not None and tracks:
            holder, granted_ms = self._pending
            target = max(tracks, key=lambda t: t.height)
            session = self._sessions.setdefault(target.track_id, Session())
            if session.holder is None:
                session.holder, session.granted_ms = holder, granted_ms
                LOG.info(
                    "auth_granted",
                    track_id=target.track_id,
                    holder=holder,
                    valid_s=self._valid_ms // 1000,
                    reason="deferred_attach",
                )
            self._pending = None

    def observe(self, markers: Sequence[Marker], tracks: Sequence[Track], now_ms: int) -> Outcome:
        """마커와 대상을 넣고 판정 하나를 낸다. 여러 마커가 보이면 승인이 우선이다."""
        if not markers:
            return Outcome.NOTHING
        outcome = Outcome.NOTHING
        loose_badge = False
        for marker in markers:
            if not self._bind_to_track:
                result = self._judge(marker, None, now_ms)
                if result is Outcome.GRANTED:
                    return result
                if result is not Outcome.NOTHING:
                    outcome = result
                continue
            track = self._owner(marker, tracks)
            if track is None and len(tracks) == 1:
                track = tracks[0]  # 한 명뿐이면 박스 밖이라도 그 사람이다
            if track is None:
                if marker.marker_id in self._badges:
                    loose_badge = True
                    self._pending = (self._badges[marker.marker_id], now_ms)
                continue
            result = self._judge(marker, track, now_ms)
            if result is Outcome.GRANTED:
                return result
            if result is not Outcome.NOTHING:
                outcome = result
        if loose_badge and outcome is Outcome.NOTHING:
            return Outcome.BADGE_SEEN
        return outcome

    def _judge(self, marker: Marker, track: Track | None, now_ms: int) -> Outcome:
        track_id = track.track_id if track is not None else 0
        session = self._sessions.setdefault(track_id, Session())
        if self.holder(track_id, now_ms) is not None:
            return Outcome.NOTHING  # 이미 유효한 세션이 있다
        if marker.marker_id in session.seen:
            return Outcome.NOTHING  # 같은 사원증을 다시 본 것은 새 시도가 아니다
        holder = self._badges.get(marker.marker_id)
        if holder is None and not self._proven(marker.marker_id, now_ms):
            return Outcome.NOTHING  # 아직 안정 검출이 아니다 — 시도로 세지 않는다
        session.seen.add(marker.marker_id)
        self._unproven.pop(marker.marker_id, None)
        if holder is not None:
            session.holder = holder
            session.granted_ms = now_ms
            session.seen.clear()  # 다음 만료 뒤에는 다시 시도할 수 있어야 한다
            LOG.info(
                "auth_granted",
                track_id=track.track_id if track is not None else None,
                marker_id=marker.marker_id,
                holder=holder,
                valid_s=self._valid_ms // 1000,
            )
            return Outcome.GRANTED
        exhausted = session.attempts >= self._max_attempts
        LOG.warning(
            "auth_rejected",
            track_id=track.track_id if track is not None else None,
            marker_id=marker.marker_id,
            attempts=session.attempts,
            max_attempts=self._max_attempts,
            exhausted=exhausted,
        )
        return Outcome.EXHAUSTED if exhausted else Outcome.REJECTED

    def _proven(self, marker_id: int, now_ms: int) -> bool:
        """미등록 마커가 1초 창 안에 `unknown_marker_min_frames` 회 읽혔는가. 창이 끊기면 다시 센다."""
        count, last = self._unproven.get(marker_id, (0, 0))
        count = count + 1 if now_ms - last <= 1000 else 1
        self._unproven[marker_id] = (count, now_ms)
        return count >= self._min_unknown_frames

    def _owner(self, marker: Marker, tracks: Sequence[Track]) -> Track | None:
        """마커를 든 사람 — 마커 중심을 품은 박스 중 가장 큰 것. `bind_to_track_id` 가 꺼져
        있으면 가장 큰 대상이다."""
        if not tracks:
            return None
        if not self._bind_to_track:
            return max(tracks, key=lambda t: t.height)
        x, y = marker.center
        holding = [
            track
            for track in tracks
            if track.box[0] <= x <= track.box[2] and track.box[1] <= y <= track.box[3]
        ]
        if not holding:
            return None
        # 둘 이상이 포함하면 가까운(박스가 큰) 쪽 (FR-3.8.2).
        return max(holding, key=lambda t: t.height)
