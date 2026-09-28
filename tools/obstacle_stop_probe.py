"""온보드 근거리 반사 정지를 실기에서 확인한다 (WBS 3.2.6).

    python tools/obstacle_stop_probe.py --host 192.168.1.101
    python tools/obstacle_stop_probe.py --host 192.168.1.101 --serial COM9

**무엇을 보는가.** 초음파가 `safety.obstacle_stop_cm` 미만을 보면 펌웨어가
호스트를 기다리지 않고 멈추고, 그동안 **전진만** 거부하는지 본다. 후진이 막히면
`FR-2.3` 의 *"정지 후 후진"* 이 실행 불가가 되므로 그것도 함께 확인한다.

⚠️ **로봇을 받침대에 올려 다리가 공중에 뜨게 둔다.** 이 도구는 전진·후진 명령을
실제로 보낸다. 바닥에 세워 두면 기체가 이동한다.

⚠️ 관제 런타임(`host.runtime`)이 떠 있으면 명령 seq 가 경합하고 텔레메트리 포트도
겹친다. 측정 중에는 내려 둔다.

`--serial` 을 주면 UART 로그에서 `Obstacle stop: dist=..cm sample_age=..ms` 줄을
함께 모은다 — **«감지 → 정지» 를 온보드 시각으로 입증하는 근거**다(DoD ①).
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from service_action_probe import Tap, ack_for, pump  # noqa: E402

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402

#: 구동 창. 한 걸음 남짓이면 ACK 판정에 충분하다 — 길게 돌릴 이유가 없다.
DRIVE_S = 1.0
#: 상태를 확인하는 관찰 창. 센서 40ms · 텔레메트리 100ms 주기라 넉넉하다.
OBSERVE_S = 2.0

_STOP_LINE = re.compile(r"Obstacle stop: dist=([\d.]+)cm sample_age=(\d+)ms")


def parse_stop_lines(text: str) -> list[tuple[float, int]]:
    """UART 로그에서 온보드 정지 기록을 뽑는다. (거리cm, 표본나이ms).

    소켓·시리얼과 떼어 둔 이유는 판정이 여기 있기 때문이다 — 나이가 곧
    «감지에서 정지까지» 의 온보드 상한이다 (CONTRIBUTING 5.2 HAL 분리).
    """
    return [(float(d), int(age)) for d, age in _STOP_LINE.findall(text)]


class SerialLog:
    """UART 로그 수집기. `--serial` 을 주지 않으면 아무 일도 하지 않는다."""

    def __init__(self, port: str | None) -> None:
        self.text = ""
        self._port = None
        if not port:
            return
        import serial  # pyserial — 없으면 여기서야 죽는다

        # ⚠️ DTR/RTS 를 건드리면 보드가 리셋된다. 읽기만 한다.
        self._port = serial.Serial()
        self._port.port = port
        self._port.baudrate = 115200
        self._port.timeout = 0
        self._port.dtr = False
        self._port.rts = False
        self._port.open()

    def drain(self) -> None:
        if self._port is None:
            return
        pending = self._port.read(8192)
        if pending:
            self.text += pending.decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._port is not None:
            self._port.close()


def step(title: str, prompt: str | None = None, *, interactive: bool = True) -> None:
    """단계 안내. `--phase` 로 한 단계만 돌릴 때는 물어보지 않는다 — 물체를 옮기는
    사람과 도구를 돌리는 사람이 다를 수 있다."""
    print(f"\n{title}")
    if prompt and interactive:
        input(f"  {prompt} — 준비되면 엔터: ")
    elif prompt:
        print(f"  (전제: {prompt})")


def drive(sock, peer, commander: Commander, step_mm: float, acks: list[dict]) -> dict | None:
    """전진/후진 한 창. 보낸 `MOVE` **자체의** ACK 를 돌려준다."""
    acks.clear()
    commander.drive(step_mm, 0.0)
    pump(sock, peer, commander, DRIVE_S, acks)
    commander.halt()
    pump(sock, peer, commander, 0.3, acks)
    return ack_for(acks, "MOVE")


def main() -> int:
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description="근거리 반사 정지 실기 확인 (3.2.6)")
    ap.add_argument("--device", default="mechdog-01")
    ap.add_argument("--host", required=True, help="로봇 IP")
    ap.add_argument("--serial", default=None, help="UART 로그 포트 (예: COM9)")
    ap.add_argument("--step-mm", type=float, default=60.0, help="구동 창의 보폭")
    ap.add_argument(
        "--phase",
        choices=("all", "clear", "blocked", "released"),
        default="all",
        help="한 단계만 돌린다 — 물체를 옮기는 사이에 나눠 실행할 때",
    )
    args = ap.parse_args()
    phase = args.phase
    interactive = phase == "all"

    config = load_config(args.device)
    network = config["network"]
    stop_cm = float(config["safety"]["obstacle_stop_cm"])
    peer = (args.host, int(network["cmd_port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=100)
    tap = Tap(int(network["telemetry_port"]))
    log = SerialLog(args.serial)
    acks: list[dict] = []
    checks: list[tuple[bool, str]] = []

    def observe(seconds: float) -> None:
        pump(sock, peer, commander, seconds, acks)
        log.drain()

    def report() -> str:
        return (
            f"dist={tap.field('dist_cm')}cm state={tap.field('state')} "
            f"obstacle={tap.field('obstacle')} latched={tap.field('safety_latched')}"
        )

    try:
        sock.sendto(commander.open_session().encode("utf-8"), peer)
        commander.halt()
        observe(2.0)
        if not tap.rows:
            print("텔레메트리가 오지 않는다 — 주소·전원·다른 런타임 점유를 확인한다")
            return 2
        print(f"기준선: {report()} batt={tap.field('batt_v')}V")
        if tap.field("obstacle") is None:
            print("⚠️ 이 펌웨어는 flags.obstacle 을 보고하지 않는다 — 옛 이미지다")
            return 3

        # ⚠️ **어느 단계로 들어와도 래치부터 푼다.** 이 도구는 끝낼 때 ESTOP 을
        # 보내므로, 단계를 나눠 돌리면 다음 단계가 래치 때문에 거부된다 — 그것을
        # «장애물이 막았다» 로 읽으면 판정이 통째로 거짓이 된다.
        commander.once("RESET_SAFE")
        observe(1.0)
        print(f"  래치 해제: latched={tap.field('safety_latched')}")

        if phase in ("all", "clear"):
            step(
                "[1] 전방을 비운 상태",
                f"센서 앞 {stop_cm * 2:.0f}cm 이상을 비웁니다",
                interactive=interactive,
            )
            clear_move = drive(sock, peer, commander, args.step_mm, acks)
            log.drain()
            print(f"  전진 ACK={clear_move}")
            print(f"  {report()}")
            checks += [
                (tap.field("obstacle") is False, "비어 있을 때 obstacle=false"),
                (
                    bool(clear_move) and clear_move.get("applied") is True,
                    "비어 있을 때 전진이 나간다",
                ),
            ]

        if phase in ("all", "blocked"):
            step(
                "[2] 장애물 접근",
                f"센서 앞 {stop_cm * 0.7:.0f}cm 쯤에 평평한 물체를 둡니다",
                interactive=interactive,
            )
            observe(OBSERVE_S)
            print(f"  {report()}")
            checks += [
                (tap.field("obstacle") is True, "장애물을 보면 obstacle=true"),
                (tap.field("state") == "AVOID", "state 가 AVOID 로 보고된다"),
            ]

            step("[3] 장애물 앞에서 전진 — 거부되어야 한다")
            blocked = drive(sock, peer, commander, args.step_mm, acks)
            log.drain()
            print(f"  전진 ACK={blocked}")
            checks.append(
                (bool(blocked) and blocked.get("applied") is False, "장애물 앞에서 전진이 막힌다")
            )

            step("[4] 같은 자리에서 후진 — 통과해야 한다 (FR-2.3 탈출로)")
            back = drive(sock, peer, commander, -args.step_mm, acks)
            log.drain()
            print(f"  후진 ACK={back}")
            checks.append((bool(back) and back.get("applied") is True, "후진은 막히지 않는다"))

        if phase in ("all", "released"):
            step("[5] 장애물 제거", "물체를 치웁니다", interactive=interactive)
            observe(OBSERVE_S)
            print(f"  {report()}")
            checks += [
                (tap.field("obstacle") is False, "치우면 obstacle=false 로 풀린다"),
                (tap.field("state") != "AVOID", "AVOID 보고가 끝난다"),
            ]

            recovered = drive(sock, peer, commander, args.step_mm, acks)
            log.drain()
            print(f"  전진 ACK={recovered}")
            checks.append(
                (bool(recovered) and recovered.get("applied") is True, "해제 뒤 전진이 다시 나간다")
            )

        stops = parse_stop_lines(log.text)
        if stops:
            worst = max(age for _dist, age in stops)
            print(f"\n온보드 정지 기록 {len(stops)}건 · 표본 나이 최대 {worst}ms")
            for dist_cm, age in stops:
                print(f"  dist={dist_cm}cm sample_age={age}ms")
            checks.append((worst <= 50, f"«감지 → 정지» 가 50ms 이하다 (최대 {worst}ms)"))
        elif args.serial:
            print("\n⚠️ UART 에서 정지 기록을 찾지 못했다 — 포트를 다른 프로그램이 잡고 있나")
    except KeyboardInterrupt:
        print("\n중단됐다")
        return 130
    finally:
        sock.sendto(commander.emergency_stop().encode("utf-8"), peer)
        sock.close()
        tap.close()
        log.close()

    print("\n판정")
    for ok, label in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
    failed = [label for ok, label in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 통과")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
