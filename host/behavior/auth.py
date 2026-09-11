"""사원증 ArUco 인증 (WBS 3.8.1 · FR-10.1).

사원증에 인쇄된 **ArUco 마커**를 읽어 승인 인원을 식별한다. 로고 템플릿 매칭은
조명·각도에 취약하고 전용 모델 학습은 데이터 수집 비용이 크다 — 사원증에 코드가
인쇄된 형태는 현실의 출입 통제와 같으므로 타당하다(FR-10.1 설계 근거).

두 층으로 나눈다. **파일도 나눈다** — `worker` 가 `behavior` 를 import 하게 두면
방향이 거꾸로가 되기 때문이다.

  ① `vision/badge.py` — 픽셀에서 마커를 찾는다. `cv2` 를 쓰는 유일한 곳이다.
  ② 이 파일          — 누구의 인증인지 정하고 세션을 들고 있다. **픽셀을 모른다.**

⚠️ **인증은 추적 ID 에 귀속된다** (FR-3.6.2). 여러 명일 때 누가 인증됐는지
구분해야 하고, 추적 ID 가 사라지면 그 세션도 만료된다(FR-3.6.3). 그래서 이
모듈은 `Track` 을 받는다 — 인증은 *프레임*의 속성이 아니라 *사람*의 속성이다.

⚠️ **마커를 누구에게 귀속시킬지는 기하로 정한다.** 마커 중심이 어느 추적 박스
안에 있는지를 본다. 사원증은 몸 앞에 들기 때문이다. 둘 이상이 포함하면 박스가
더 큰(가까운) 쪽이다 — FR-3.8.2 와 같은 단일 기준이다.

**아무 박스에도 들지 않은 마커는 무시한다.** 벽에 붙은 마커나 화면에 떠 있는
마커가 아무도 없는 방에서 인증을 만들어 내면 안 된다.

⚠️ **같은 마커를 반복해서 봐도 시도 횟수는 한 번이다.** 25fps 로 도는데 프레임마다
세면 `max_attempts: 2` 가 **80ms 에 소진된다** — 사원증을 한 번 들어 보이는 것이
곧 "2회 실패"가 된다. 프레임 수로 무언가를 세다 실패한 것이 이 저장소에 이미
세 번 있다(결정 22·25·27번). 그래서 **본 마커 ID 의 집합**으로 센다. 다른
사원증을 두 번째로 들어 보이는 것이 두 번째 시도다.

같은 사원증을 계속 들고 있는 경우는 이 규칙으로 잡히지 않지만, **`AUTH_WAIT` 의
30초 타이머가 그 경로를 덮는다**(FR-10.3). 두 장치가 서로를 보완하므로 여기에
새 시간 상수를 만들지 않는다.
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
    """관측 한 번의 결과. **호출부가 사건으로 옮긴다.**"""

    NOTHING = "nothing"  # 볼 것이 없었다
    GRANTED = "granted"  # 승인 — `AUTH_OK`
    REJECTED = "rejected"  # 등록되지 않은 사원증. 기회가 남았다
    EXHAUSTED = "exhausted"  # 시도 횟수 초과 — `AUTH_FAILED` (FR-10.3)


@dataclass(slots=True)
class Session:
    """한 사람의 인증 상태. 추적 ID 하나에 하나씩."""

    holder: str | None = None
    granted_ms: int | None = None
    #: 이미 판정한 마커 ID 들. **집합인 것이 시도 횟수 규칙이다** (모듈 주석).
    seen: set[int] = field(default_factory=set)

    @property
    def attempts(self) -> int:
        return len(self.seen)


class Authenticator:
    """마커를 사람에게 귀속시키고 세션을 들고 있다. **시계를 만들지 않는다.**"""

    def __init__(self, config: Mapping[str, Any]) -> None:
        auth = config["auth"]
        # ⚠️ YAML 키가 정수로 읽히든 문자열로 읽히든 같게 동작해야 한다 — 설정을
        # 손으로 고치는 사람이 `0:` 과 `"0":` 를 구분해 쓸 이유가 없다.
        self._badges = {
            int(key): str(value) for key, value in (auth["badge_marker_map"] or {}).items()
        }
        self._valid_ms = int(auth["session_valid_s"]) * 1000
        self._max_attempts = int(auth["max_attempts"])
        self._bind_to_track = bool(auth["bind_to_track_id"])
        self._sessions: dict[int, Session] = {}

    # ── 상태 ────────────────────────────────────────────────
    def holder(self, track_id: int, now_ms: int) -> str | None:
        """그 대상의 인증된 신분. 유효하지 않으면 `None`."""
        session = self._sessions.get(track_id)
        if session is None or session.granted_ms is None:
            return None
        if now_ms - session.granted_ms >= self._valid_ms:
            return None  # FR-10.2.4 — 유효 시간이 지났다
        return session.holder

    def attempts(self, track_id: int) -> int:
        session = self._sessions.get(track_id)
        return 0 if session is None else session.attempts

    def all_authenticated(self, tracks: Sequence[Track], now_ms: int) -> bool:
        """보이는 **전원**이 인증됐나 (FR-3.8.1).

        ⚠️ **한 명이라도 미인증이면 False 다.** 실제 출입 통제와 같고 안전측이다 —
        인증된 사람 뒤에 미인증자가 따라 들어오는 것이 막아야 하는 상황이다.
        """
        if not tracks:
            return False
        return all(self.holder(track.track_id, now_ms) is not None for track in tracks)

    # ── 관측 ────────────────────────────────────────────────
    def note_tracks(self, tracks: Sequence[Track]) -> None:
        """지금 보이는 대상들을 알린다. **사라진 ID 의 세션을 만료시킨다** (FR-3.6.3).

        ⚠️ 세션을 남겨 두면 같은 번호를 다시 받은 사람이 물려받는다 — 추적기가
        ID 를 재사용하지 않는 것(`3.3.4`)과 같은 이유로 여기서도 지운다.
        """
        alive = {track.track_id for track in tracks}
        for track_id in list(self._sessions):
            if track_id not in alive:
                del self._sessions[track_id]
                LOG.info("auth_session_dropped", track_id=track_id, reason="track_lost")

    def observe(self, markers: Sequence[Marker], tracks: Sequence[Track], now_ms: int) -> Outcome:
        """마커와 대상을 넣고 판정 하나를 낸다.

        여러 마커가 보이면 **승인이 우선**이다 — 승인된 사원증을 든 사람이 있는데
        옆의 모르는 마커 때문에 경보로 가면 안 된다.
        """
        if not markers or not tracks:
            return Outcome.NOTHING
        outcome = Outcome.NOTHING
        for marker in markers:
            track = self._owner(marker, tracks)
            if track is None:
                continue  # 아무 박스에도 들지 않은 마커 — 벽이나 화면이다
            result = self._judge(marker, track, now_ms)
            if result is Outcome.GRANTED:
                return result
            if result is not Outcome.NOTHING:
                outcome = result
        return outcome

    def _judge(self, marker: Marker, track: Track, now_ms: int) -> Outcome:
        session = self._sessions.setdefault(track.track_id, Session())
        if self.holder(track.track_id, now_ms) is not None:
            return Outcome.NOTHING  # 이미 유효한 세션이 있다
        if marker.marker_id in session.seen:
            return Outcome.NOTHING  # 같은 사원증을 다시 본 것은 새 시도가 아니다
        session.seen.add(marker.marker_id)
        holder = self._badges.get(marker.marker_id)
        if holder is not None:
            session.holder = holder
            session.granted_ms = now_ms
            session.seen.clear()  # 다음 만료 뒤에는 다시 시도할 수 있어야 한다
            LOG.info(
                "auth_granted",
                track_id=track.track_id,
                marker_id=marker.marker_id,
                holder=holder,
                valid_s=self._valid_ms // 1000,
            )
            return Outcome.GRANTED
        exhausted = session.attempts >= self._max_attempts
        LOG.warning(
            "auth_rejected",
            track_id=track.track_id,
            marker_id=marker.marker_id,
            attempts=session.attempts,
            max_attempts=self._max_attempts,
            exhausted=exhausted,
        )
        return Outcome.EXHAUSTED if exhausted else Outcome.REJECTED

    def _owner(self, marker: Marker, tracks: Sequence[Track]) -> Track | None:
        """마커를 든 사람. **중심이 박스 안에 있는가**로 정한다.

        ⚠️ `bind_to_track_id` 가 꺼져 있으면 대상 하나에 몰아 준다 — 추적 없이
        운용하는 진단용 경로이며, 그때는 여러 명을 구분할 수 없다.
        """
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
        # 둘 이상이 포함하면 가까운(박스가 큰) 쪽 — FR-3.8.2 와 같은 단일 기준이다.
        return max(holding, key=lambda t: t.height)
