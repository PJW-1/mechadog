"""고정 주기 명령 송신기 (WBS 4.3.2 · FR-5.1).

**변화가 없어도 계속 보낸다.** 로봇은 `cmd_timeout_ms`(300ms) 동안 명령이 없으면
스스로 멈추므로, 같은 명령을 10Hz 로 계속 보내는 것이 곧 *"링크가 살아 있다"* 는
신호다. 그래서 별도 하트비트를 두지 않는다 (PROTOCOL.md 1절).

**이 모듈은 소켓을 만지지 않는다.** 무엇을 보낼지만 정하고 실제 전송은 호출자가
한다 (ENGINEERING_GUIDE 2.1). 그래야 하드웨어 없이 주기·순서·클램핑을 전부
pytest 로 검증할 수 있다.

주기는 `sleep(0.1)` 이 아니라 **다음 발사 시각을 절대 시각으로 누적**한다.
`sleep` 방식은 처리 시간이 매 주기 누적돼 실제 주기가 느려지고, 그러면 로봇의
300ms 타임아웃이 의도보다 먼저 걸린다 — 첫 안전 시험에서 실제로 겪은 일이다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from host.common.protocol import CommandEncoder, system_clock_ms


@dataclass(frozen=True, slots=True)
class Intent:
    """**반복해서 보낼 의도.** 한 번만 보낼 것은 여기 담지 않는다."""

    type_: str
    fields: Mapping[str, Any] = field(default_factory=dict)


#: 아무 지시가 없을 때의 기본 의도. **무입력은 정지로 수렴한다** — 마지막 명령을
#: 계속 실행하게 두면 조작자가 손을 뗀 뒤에도 로봇이 걸어간다.
HALT = Intent("STOP")


class Commander:
    """의도를 들고 있다가 고정 주기로 같은 명령을 계속 내보낸다.

    `drive`·`halt` 는 **즉시 보내지 않는다.** 다음 틱에 반영된다. 즉시 나가야 하는
    것(비상정지)은 별도 메서드가 완성된 전문을 돌려주므로 호출자가 그 자리에서
    보낸다 — 100ms 를 기다리게 만들면 안 되는 유일한 부류다.
    """

    def __init__(
        self,
        encoder: CommandEncoder | None = None,
        *,
        period_ms: int = 100,
        clock: Callable[[], int] = system_clock_ms,
    ) -> None:
        if period_ms <= 0:
            raise ValueError("period_ms 는 1 이상이어야 함")
        self._encoder = encoder if encoder is not None else CommandEncoder(clock=clock)
        self._period_ms = period_ms
        self._intent: Intent = HALT
        self._pending: list[Intent] = []
        self._announced: str | None = None
        self._next_due: int | None = None
        self._ticks = 0

    # ── 조회 ───────────────────────────────────────────────────
    @property
    def intent(self) -> Intent:
        return self._intent

    @property
    def period_ms(self) -> int:
        return self._period_ms

    @property
    def ticks(self) -> int:
        """실제 발사된 주기 횟수. 시험에서 주기 정확도를 볼 때 쓴다."""
        return self._ticks

    @property
    def announced_state(self) -> str | None:
        return self._announced

    # ── 반복 의도 ──────────────────────────────────────────────
    def drive(self, step: float, angle: float) -> None:
        """보행 의도를 세운다. 범위 초과는 인코더가 잘라낸다 (규칙 ②)."""
        self._intent = Intent("MOVE", {"step": step, "angle": angle})

    def halt(self) -> None:
        self._intent = HALT

    # ── 한 번만 보낼 것 ────────────────────────────────────────
    def announce(self, state: str) -> None:
        """호스트 FSM 상태를 로봇에게 알린다. **값이 바뀔 때만 1회** 보낸다.

        로봇은 이 값을 판단 근거로 쓰지 않고 받아적어 텔레메트리로 되돌려준다.
        매 틱 보내면 대역만 먹고, 안 보내면 텔레메트리의 `state` 를 아무도 채울 수
        없다 (PROTOCOL.md `STATE` 절).
        """
        if state == self._announced:
            return
        self._pending.append(Intent("STATE", {"state": state}))
        self._announced = state

    def once(self, type_: str, **fields: Any) -> None:
        """다음 틱에 1회만 실어 보낸다 (`LED`·`SOUND`·`ACTION` 용)."""
        self._pending.append(Intent(type_, dict(fields)))

    def emergency_stop(self) -> str:
        """**즉시 보낼 `ESTOP` 전문을 돌려준다.** 반복 의도도 정지로 내린다.

        틱을 기다리지 않는 유일한 명령이다. 그리고 래치가 걸린 뒤에는 `MOVE` 가
        어차피 차단되므로, 의도를 정지로 바꿔 두는 것이 실제 상태와 맞는다.
        """
        self.halt()
        return self._encoder.estop()

    def open_session(self) -> str:
        """새 호스트 프로세스의 **첫 전문** (`STOP` seq=1).

        ⚠️ **프로세스에서 가장 먼저 인코딩해야 한다.** 로봇은 seq 역전을 폐기하므로,
        재시작해서 seq 가 1 로 돌아간 호스트는 이전 세션의 최대 seq 를 넘을 때까지
        **통째로 무시된다** — 10Hz 로 10분 운용했다면 그 뒤 10분 동안 `ESTOP` 조차
        닿지 않는다. 규약은 그래서 **`STOP` seq=1 + 더 새로운 ts** 를 세션 개시
        신호로 정해 두었다 (PROTOCOL 4절). 이 전문이 정확히 그 신호다.

        `STATE` 나 `RESET_SAFE` 가 먼저 나가면 신호가 성립하지 않는다. 실제로
        `--reset-on-start` 로 첫 전문이 `RESET_SAFE` 가 되어 목업이 우리 명령을
        159건 폐기했다.
        """
        self.halt()
        return self._encoder.encode("STOP")

    def clear_safe(self) -> str:
        """**즉시 보낼 `RESET_SAFE` 전문을 돌려준다.**

        원인을 해소하고 **사람이 확인한 뒤에만** 호출한다. 자동 해제를 만들면
        무엇 때문에 멈췄는지 모르는 채로 다시 움직인다 (DR-16).
        """
        self.halt()
        return self._encoder.reset_safe()

    # ── 주기 송신 ──────────────────────────────────────────────
    @property
    def next_due_ms(self) -> int | None:
        """다음 송신 예정 시각. `None` 이면 아직 첫 틱을 돌지 않았다.

        운용 루프가 소켓을 얼마나 기다려도 되는지 계산하는 데 쓴다. 루프가 자기
        마감을 따로 세면 두 개의 시계가 생기고 언젠가 어긋난다.
        """
        return self._next_due

    def due(self, now_ms: int) -> bool:
        return self._next_due is None or now_ms >= self._next_due

    def tick(self, now_ms: int) -> list[str]:
        """발사 시각이면 보낼 전문 목록을 돌려준다. 아니면 빈 목록.

        한 번만 보낼 것이 먼저 나가고 반복 의도가 마지막에 나간다. 순서가
        중요하다 — `STATE` 를 알린 다음 그 상태에 맞는 동작을 보내야 로봇의
        텔레메트리와 실제 동작이 어긋나 보이지 않는다.
        """
        if not self.due(now_ms):
            return []

        if self._next_due is None:
            self._next_due = now_ms + self._period_ms
        else:
            self._next_due += self._period_ms
            # 한 주기 이상 밀렸으면 과거를 따라잡지 않고 지금 기준으로 재동기한다.
            # 밀린 만큼 몰아 보내면 순간 폭주가 되고, 로봇 입장에서는 오래된
            # 명령이 줄줄이 들어오는 것이라 더 나쁘다.
            if self._next_due <= now_ms:
                self._next_due = now_ms + self._period_ms

        out = [self._encoder.encode(i.type_, **dict(i.fields)) for i in self._pending]
        self._pending.clear()
        out.append(self._encoder.encode(self._intent.type_, **dict(self._intent.fields)))
        self._ticks += 1
        return out
