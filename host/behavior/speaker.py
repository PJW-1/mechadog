"""말하기 경로 (ADR-38) — 상황 문장은 관제 방송으로, 사건 키는 로봇 MP3 트랙으로.

`SOUND` 는 한 번만 나가는 전문이라 ACK 대기·재전송과 경고 반복도 여기서 맡는다.
모든 메서드는 운용 루프 스레드에서 부른다 (`Runtime.tick`·`ingest`·`IncidentLog.record_scene`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from host.behavior.commander import Commander
from host.common.logging_setup import event_logger
from host.report.situation import describe

if TYPE_CHECKING:
    from host.behavior.patrol import PatrolController
    from host.vision.worker import VisionResult

#: 런타임과 같은 로거 이름을 쓴다 — 로그 레코드가 옮기기 전과 같아야 한다.
LOG = event_logger("mechadog.runtime")

# `SOUND` ACK 대기 — 실측 왕복은 30ms 안팎(`last_cmd_age_ms`)이라 세 주기면 넉넉하다.
SOUND_ACK_TIMEOUT_MS = 300
SOUND_RETRIES = 2
# `SOUND.track` 상한 (PROTOCOL `SOUND` 절 · `dashboard.commands.SOUND_TRACK_MAX` 와 같은 값).
ROBOT_SOUND_TRACK_MAX = 3000
# 안내 문장은 한 번만 — 반복은 놓치면 안 되는 경고에만 건다 (`robot_sound.repeat_after_ms`).
ROBOT_SOUND_NO_REPEAT = frozenset(
    {"route_started", "route_finished", "alarm_confirmed", "ppe_settled", "fall_suspected"}
)


class Speaker:
    """사건 문장 방송과 로봇 스피커 트랙 재생·반복·재전송을 맡는다."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        commander: Commander,
        clock: Callable[[], int],
        announcer: Callable[[str], None] | None,
        latest: Callable[[], VisionResult | None],
    ) -> None:
        self._commander = commander
        self._clock = clock
        #: 상황 서술 문장을 관제로 내보내는 방송기. 비동기·예외를 던지지
        #: 않는 계약이지만 `announce` 에서 다시 한 번 감싼다 — 아직 없는 계약을
        #: 믿고 안 감싸면 방송기가 하나라도 어기는 순간 제어 틱이 죽는다.
        self._announcer = announcer
        #: 보호구 경고 키를 고를 때 읽는 최신 추론 결과 (`ppe_warning_key`).
        self._latest = latest
        #: 사건 → 로봇 MP3 모듈 TF 카드 트랙 (`robot_sound.tracks` · ADR-38 말하기 경로).
        #: 키는 사건 이름(`person_fallen` 등)이나 경고 키(`ppe_violation_warning`). 비면 안 튼다.
        self.tracks = {
            str(k): int(v)
            for k, v in ((config.get("robot_sound") or {}).get("tracks") or {}).items()
        }
        #: 같은 경고를 이만큼 뒤 한 번 더 튼다(0 이면 안 함). 로봇은 모듈에 쓰기 전에 ACK 를
        #: 보내므로 재생 실패를 알 수 없다 — 2026-10-06 리허설에서 ACK 는 왔는데 무음이었다.
        self._repeat_ms = int((config.get("robot_sound") or {}).get("repeat_after_ms") or 0)
        self._repeat: tuple[int, int] | None = None  # (시각, 트랙)
        # ACK 를 기다리는 `SOUND` 하나 — (seq, track, 다시 보낼 시각, 남은 재전송) (`resend`).
        self._wait: tuple[int, int, int, int] | None = None
        self._retries_left = SOUND_RETRIES
        #: 동선 시작·완료 안내용 직전 상태 (`route_edges`).
        self._route_was_active = False

    def announce(self, event_type: str, judgement: dict[str, Any] | None) -> str | None:
        """상황 문장을 만들어 방송한다. 만든 문장(없으면 `None`)을 돌려준다."""
        sentence: str | None = None
        try:
            sentence = describe(event_type, judgement)
        except Exception as exc:  # noqa: BLE001 — 문장 생성 실패가 10Hz 제어를 죽이면 안 된다
            LOG.error("situation_failed", error=f"{type(exc).__name__}: {exc}")
        if sentence is not None and self._announcer is not None:
            try:
                self._announcer(sentence)
            except Exception as exc:  # noqa: BLE001 — 방송 실패가 제어를 막으면 안 된다
                LOG.error("announce_failed", error=f"{type(exc).__name__}: {exc}")
        if sentence is not None:
            key = event_type
            items = (judgement or {}).get("items")
            if (
                event_type == "hazard_notice"
                and (judgement or {}).get("source") == "detector"
                and isinstance(items, list)
                and len(set(items)) == 1
                and f"hazard_notice_{items[0]}" in self.tracks
            ):
                key = f"hazard_notice_{items[0]}"
            self.play(key)
        return sentence

    def play(self, key: str) -> None:
        """`key` 에 맞는 TF 카드 트랙을 로봇 스피커로 튼다. 표에 없으면 아무것도 안 한다.

        ACK 가 없으면 `resend` 가 새 seq 로 다시 싣는다.
        """
        track = self.tracks.get(key)
        if track is None or not 0 < track <= ROBOT_SOUND_TRACK_MAX:
            return
        # 새 소리가 나가면 이전 경고의 반복은 취소한다 — 경보 확인 뒤 옛 경고가 다시 나오지 않게
        # (10-06 현장 검수).
        self._repeat = None
        try:
            self._commander.once("SOUND", track=track)
            LOG.info("robot_sound", key=key, track=track)
            if self._repeat_ms > 0 and key not in ROBOT_SOUND_NO_REPEAT:
                self._repeat = (self._clock() + self._repeat_ms, track)
        except Exception as exc:  # noqa: BLE001 — 소리 실패가 제어를 막으면 안 된다
            LOG.error("robot_sound_failed", error=f"{type(exc).__name__}: {exc}")

    def ppe_warning_key(self, base: str) -> str:
        """빠진 보호구가 하나면 그것만 말하는 키(`ppe_violation_helmet_warning` 등)를 고른다.

        표에 그 키가 없거나 둘 다 빠졌거나 판정을 못 읽으면 통합 문장 키(`base`)를 쓴다.
        """
        result = self._latest()
        verdict = getattr(result, "ppe", None)
        regions = getattr(verdict, "regions", ()) or ()
        missing = {r.item for r in regions if str(r.label).startswith("no_")}
        if len(missing) == 1:
            key = f"ppe_violation_{next(iter(missing))}_warning"
            if key in self.tracks:
                return key
        return base

    def route_edges(self, navigator: PatrolController) -> None:
        """동선 시작·완료를 로봇 스피커로 알린다 (`route_started`·`route_finished`). 정지·교체는 말하지 않는다."""
        active = navigator.route_active
        if active and not self._route_was_active:
            self.play("route_started")
        elif self._route_was_active and not active:
            status = (navigator.route_status() or {}).get("status")
            if isinstance(status, str) and status.startswith("completed"):
                self.play("route_finished")
        self._route_was_active = active

    def repeat(self, now_ms: int) -> None:
        """예약한 경고 반복(`robot_sound.repeat_after_ms`)이 됐으면 한 번 더 튼다."""
        if self._repeat is not None and now_ms >= self._repeat[0]:
            track = self._repeat[1]
            self._repeat = None
            self._commander.once("SOUND", track=track)
            LOG.info("robot_sound_repeat", track=track)

    def note_ack(self, raw: str | bytes) -> None:
        """명령 응답이 기다리던 `SOUND` 의 것이면 대기를 푼다."""
        if self._wait is not None and json.loads(raw).get("seq") == self._wait[0]:
            self._wait = None

    def watch(self, lines: list[str], now_ms: int) -> None:
        """이번 틱에 나간 `SOUND` 를 ACK 대기에 올린다. 새 것이 옛 것을 밀어낸다."""
        for line in lines:
            msg = json.loads(line)
            if msg["type"] == "SOUND":
                due = now_ms + SOUND_ACK_TIMEOUT_MS
                self._wait = (msg["seq"], msg["track"], due, self._retries_left)
                self._retries_left = SOUND_RETRIES

    def resend(self, now_ms: int) -> None:
        """ACK 없이 마감이 지난 `SOUND` 를 **새 seq 로** 다시 싣는다.

        `SOUND` 는 한 번만 나가서 UDP 한 개가 빠지면 문장이 소리 없이 사라진다 —
        실기에서 명령의 약 1.2% 가 빠졌고 대체 문장 하나가 그렇게 나오지
        않았다. 같은 seq 는 펌웨어 순서 게이트가 거부하므로 `once` 로 다시 만든다.
        ⚠️ ACK 만 빠진 경우엔 같은 문장이 처음부터 다시 나온다 — 무음보다 낫다.
        """
        if self._wait is None or now_ms < self._wait[2]:
            return
        _seq, track, _due, left = self._wait
        self._wait = None
        # 더 새 문장이 이미 실려 있으면 옛 것은 버린다 — 뒤에 붙이면 새 문장을 덮는다.
        # 확인과 넣기는 한 덩어리다: 대시보드 스레드가 그 사이에 끼면 옛 것이 뒤에 붙었다.
        if left <= 0:
            if not self._commander.has_pending("SOUND"):
                LOG.warning("sound_unacked", track=track, retries=SOUND_RETRIES)
            return
        if self._commander.once_unless_pending("SOUND", track=track):
            self._retries_left = left - 1
