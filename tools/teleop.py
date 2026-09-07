"""키보드 수동 조작 (WBS 4.6.5 · FR-4.3).

웹 화면 없이 실물 로봇을 직접 몰아보는 도구다. 방향키(또는 WASD)로 전진·후진·
좌우 선회를 하고 **손을 떼면 정지**한다.

    python tools/teleop.py --host 192.168.0.50          # 실물
    python tools/teleop.py --host 127.0.0.1 --port 5001  # 목업 상대로 연습

⚠️ **콘솔에는 "키를 뗐다" 는 사건이 없다.** 터미널은 키가 눌릴 때만 문자를 주고
떼는 것은 알려주지 않는다. 그래서 여기서는 **OS 의 키 자동반복**을 이용한다 —
누르고 있으면 문자가 계속 들어오고, 떼면 들어오지 않으므로 `--release-ms` 동안
입력이 없으면 뗀 것으로 본다.

그 대가가 둘 있다.

1. 뗀 순간과 정지 사이에 최대 `--release-ms` 만큼 지연이 있다.
2. 첫 자동반복이 나오기까지 OS 가 250~500ms 를 쉬므로, **`--release-ms` 를 그보다
   짧게 잡으면 누르고 있는 중에도 정지가 섞인다.** 기본값 600ms 는 그 때문이다.

정확한 키업이 필요하면 웹 대시보드(`4.6.5` 이후)나 게임패드로 가야 한다. 다만
**로봇은 명령이 300ms 끊기면 스스로 멈추므로**, 이 도구가 죽어도 안전 쪽으로
수렴한다 — 그것이 이 방식을 쓸 수 있는 이유다.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import sys
import threading
import time
from collections.abc import Iterator

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from host.behavior.commander import Commander  # noqa: E402
from host.behavior.fsm import Behavior, Event  # noqa: E402

#: 키 → (전진 배수, 선회 배수). 방향키와 WASD 를 둘 다 받는다.
BINDINGS: dict[str, tuple[float, float]] = {
    "up": (1.0, 0.0),
    "down": (-1.0, 0.0),
    "left": (0.0, -1.0),
    "right": (0.0, 1.0),
    "w": (1.0, 0.0),
    "s": (-1.0, 0.0),
    "a": (0.0, -1.0),
    "d": (0.0, 1.0),
}
#: 전진하면서 선회 — 대각 입력. 제자리 회전은 불가하므로(DR-11) 선회는 항상 호다.
DIAGONALS: dict[str, tuple[float, float]] = {
    "q": (1.0, -1.0),
    "e": (1.0, 1.0),
}

HALT_KEYS = frozenset({" ", "\r", "\n"})
ESTOP_KEYS = frozenset({"x"})
RESET_KEYS = frozenset({"r"})
QUIT_KEYS = frozenset({"\x03", "\x1b", "z"})

HELP = """
  ↑ / W   전진        ↓ / S   후진
  ← / A   좌선회      → / D   우선회
  Q / E   전진 + 좌/우 선회
  스페이스  즉시 정지
  X       비상정지 (래치 — R 로만 해제)
  R       비상정지 해제 (원인을 확인한 뒤에)
  Z       종료
"""


def resolve(key: str, *, step_mm: float, turn_deg: float) -> tuple[float, float] | None:
    """키를 (보폭, 조향각) 으로 바꾼다. 이동 키가 아니면 `None`."""
    scale = BINDINGS.get(key) or DIAGONALS.get(key)
    if scale is None:
        return None
    return scale[0] * step_mm, scale[1] * turn_deg


class Teleop:
    """키 입력을 의도로 바꾼다. **소켓도 콘솔도 만지지 않는다.**

    마지막 입력 시각만 들고 있다가, 오래되면 정지로 되돌린다. 이 판정이 순수
    함수이므로 키보드 없이 pytest 로 전부 검증된다.
    """

    def __init__(
        self,
        behavior: Behavior,
        *,
        step_mm: float = 60.0,
        turn_deg: float = 20.0,
        release_ms: int = 600,
    ) -> None:
        if release_ms <= 0:
            raise ValueError("release_ms 는 1 이상이어야 함")
        self._behavior = behavior
        self._step_mm = step_mm
        self._turn_deg = turn_deg
        self._release_ms = release_ms
        self._last_key: str | None = None
        self._last_ms: int | None = None
        self._quit = False

    @property
    def quit_requested(self) -> bool:
        return self._quit

    @property
    def holding(self) -> str | None:
        return self._last_key

    def press(self, key: str, now_ms: int) -> str | None:
        """키를 넣는다. **즉시 보내야 하는 전문이 있으면 돌려준다.**

        비상정지만 즉시 나간다 — 나머지는 다음 틱에 실린다. 100ms 를 기다리게
        하면 안 되는 것과 기다려도 되는 것을 구분하는 것이 요점이다.
        """
        key = key.lower()
        if key in QUIT_KEYS:
            self._quit = True
            self._last_key = None
            self._behavior.commander.halt()
            return None
        if key in ESTOP_KEYS:
            self._last_key = None
            self._behavior.event(Event.ONBOARD_FAILSAFE)
            return self._behavior.commander.emergency_stop()
        if key in RESET_KEYS:
            self._last_key = None
            self._behavior.event(Event.RESET_CONFIRMED)
            return self._behavior.commander.clear_safe()
        if key in HALT_KEYS:
            self._last_key = None
            self._behavior.commander.halt()
            return None

        moved = resolve(key, step_mm=self._step_mm, turn_deg=self._turn_deg)
        if moved is None:
            return None
        self._last_key, self._last_ms = key, now_ms
        self._behavior.event(Event.MANUAL_ON)
        self._behavior.commander.drive(*moved)
        return None

    def tick(self, now_ms: int) -> list[str]:
        """뗀 지 오래됐으면 정지로 되돌리고, 송신기의 틱 결과를 돌려준다."""
        if self._last_ms is not None and now_ms - self._last_ms >= self._release_ms:
            self._last_key, self._last_ms = None, None
            self._behavior.commander.halt()
        return self._behavior.tick(now_ms)


# ── 여기서부터 바깥세상 ────────────────────────────────────────
def open_socket() -> socket.socket:
    """송신용 UDP 소켓. **Windows 의 헛된 오류 통보를 끈다.**

    아직 아무도 듣지 않는 포트로 보내면 ICMP Port Unreachable 이 돌아오고,
    Windows 는 그것을 다음 소켓 조작의 `ConnectionResetError` 로 돌려준다. UDP 에는
    연결이 없으므로 의미 없는 오류이며, 로봇 전원이 늦게 들어오는 것은 정상이다.

    ⚠️ `SIO_UDP_CONNRESET` 은 **파이썬 빌드에 따라 없다.** 실제로 이 개발 PC 에는
    없어서 `hasattr` 없이 부르면 `AttributeError` 로 도구가 즉사한다.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        with contextlib.suppress(OSError):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    return sock


def read_keys() -> Iterator[str]:
    """콘솔에서 키를 하나씩 내놓는다. 방향키는 이름(`up` 등)으로 정규화한다."""
    if sys.platform == "win32":
        import msvcrt

        arrows = {b"H": "up", b"P": "down", b"K": "left", b"M": "right"}
        while True:
            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                yield arrows.get(msvcrt.getch(), "")
                continue
            yield ch.decode(errors="replace")
    else:  # pragma: no cover - POSIX 경로는 CI(리눅스)에서 tty 가 없어 실행되지 않는다
        import termios
        import tty

        arrows = {"A": "up", "B": "down", "C": "left", "D": "right"}
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    nxt = sys.stdin.read(2)
                    if nxt.startswith("["):
                        yield arrows.get(nxt[1], "")
                        continue
                    yield "\x1b"
                    continue
                yield ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 실기 조작용
    ap = argparse.ArgumentParser(prog="teleop", description="키보드 수동 조작")
    ap.add_argument("--host", required=True, help="로봇 IP (목업이면 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--rate-hz", type=float, default=10.0)
    ap.add_argument("--step-mm", type=float, default=60.0)
    ap.add_argument("--turn-deg", type=float, default=20.0)
    ap.add_argument("--release-ms", type=int, default=600, help="이 시간 무입력이면 정지")
    a = ap.parse_args(argv)

    period_ms = max(1, round(1000 / a.rate_hz))
    behavior = Behavior(Commander(period_ms=period_ms))
    teleop = Teleop(behavior, step_mm=a.step_mm, turn_deg=a.turn_deg, release_ms=a.release_ms)

    sock = open_socket()
    target = (a.host, a.port)

    def send(lines: list[str]) -> None:
        for line in lines:
            try:
                sock.sendto(line.encode(), target)
            except OSError as exc:
                print(f"send 실패: {exc}", file=sys.stderr)

    keys: list[str] = []
    lock = threading.Lock()

    def pump() -> None:
        for key in read_keys():
            with lock:
                keys.append(key)

    threading.Thread(target=pump, daemon=True).start()

    print(f"{target[0]}:{target[1]} 로 {a.rate_hz:g}Hz 송신. {HELP}")
    next_send = time.monotonic()
    try:
        while not teleop.quit_requested:
            now_ms = int(time.time() * 1000)
            with lock:
                pending, keys[:] = list(keys), []
            for key in pending:
                if urgent := teleop.press(key, now_ms):
                    send([urgent])
            send(teleop.tick(now_ms))
            next_send += period_ms / 1000
            time.sleep(max(0.0, next_send - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        send([behavior.commander.emergency_stop()])
        sock.close()
    print("\n종료 — 비상정지를 보냈다. 다시 움직이려면 R 로 해제해야 한다.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
