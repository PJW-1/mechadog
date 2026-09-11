"""보행 이동량 실측 도구 (WBS 2.2.3 · FR-6.4).

    python tools/gait_calibrate.py --host 192.168.1.50 --mode forward
    python tools/gait_calibrate.py --host 192.168.1.50 --mode turn --trials 5
    python tools/gait_calibrate.py --host 127.0.0.1 --mode forward --dry-run   # 목업 연습

⚠️ **이 도구는 거리를 재지 못한다.** 로봇에 오도메트리가 없고 텔레메트리 송신도
아직 없다(`4.1.4`). 거리·각도는 **사람이 줄자와 각도기로** 재서 입력한다. 도구가
하는 일은 넷이다.

  ① 정해진 시간만 정확히 구동한다 — 10Hz 송신 창을 열고 닫는다
  ② **실제 송신 창 길이를 기록한다** (요청과 다를 수 있다)
  ③ 3회 이상 반복해 평균·표준편차를 낸다
  ④ `--write` 로 개체 프로파일에 `measured_on` 과 함께 적는다

⚠️ **왜 스톱워치로 재면 안 되는가.** 로봇은 명령이 300ms 끊기면 스스로 멈추므로
(`safety.cmd_timeout_ms`) **실제 구동 시간은 송신 창의 길이 + 최대 한 주기**다.
사람이 *"1초"* 라고 생각한 구간과 로봇이 실제로 걸은 구간은 다르고, 그 차이가
그대로 mm/s 오차가 된다. 그래서 창을 도구가 열고 닫으며 그 길이를 남긴다.

⚠️ **피치·롤 진폭(2.2.3 ③)은 이 도구로 못 낸다.** 펌웨어 텔레메트리 송신(`4.1.4`)과
IMU 드라이버(`OI-23`)가 선행이다. 자리만 만들어 두면 값이 0 으로 채워져 *"쟀다"*
로 보이므로 **아예 거부한다** — 없는 것은 없는 대로 둔다.

⚠️ **시연할 바닥에서 재야 한다.** 카펫과 장판에서 값이 다르다. 표준편차가 크게
나오면 그것이 바닥이 미끄럽다는 신호이며, 그 바닥에서 회피 시퀀스를 신뢰할 수
없다는 뜻이다.
"""

from __future__ import annotations

import argparse
import socket
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402

#: 구동 전 정지 시간. 온보드가 자세를 가라앉힐 시간을 준다 (회피 시퀀스의 `settle` 과 같은 이유).
SETTLE_S = 1.0
#: 표준편차가 평균의 이 비율을 넘으면 경고한다. 바닥이 일정하지 않다는 신호다.
SPREAD_WARN = 0.15


@dataclass(frozen=True, slots=True)
class Trial:
    """한 번의 구동. **요청 시간이 아니라 실제 송신 창을 들고 있다.**"""

    index: int
    requested_s: float
    actual_s: float
    packets: int
    measured: float  # 사람이 잰 값 (mm 또는 도)

    @property
    def rate(self) -> float:
        """단위 시간당 값. **실제 송신 창으로 나눈다.**"""
        return self.measured / self.actual_s


def drive_window(
    sock: socket.socket,
    peer: tuple[str, int],
    commander: Commander,
    *,
    step_mm: float,
    angle_deg: float,
    seconds: float,
) -> tuple[float, int]:
    """`seconds` 동안 구동 명령을 보내고 **실제 창 길이와 패킷 수**를 돌려준다.

    끝에 정지를 한 번 보낸다 — 보내지 않아도 300ms 뒤에 로봇이 멈추지만, 그
    300ms 가 측정 구간에 섞인다.
    """
    commander.drive(step_mm, angle_deg)
    packets = 0
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        now = system_clock_ms()
        for line in commander.tick(now):
            sock.sendto(line.encode("utf-8"), peer)
            packets += 1
        # ⚠️ `next_due_ms` 는 프로퍼티다 — 호출하면 `TypeError` 가 난다.
        # 다음 마감까지만 잔다. 자체 주기를 세면 시계가 둘이 된다.
        due = commander.next_due_ms
        if due is not None:
            time.sleep(max(0.0, (due - system_clock_ms()) / 1000))
    actual = time.perf_counter() - started
    commander.halt()
    for line in commander.tick(system_clock_ms()):
        sock.sendto(line.encode("utf-8"), peer)
        packets += 1
    return actual, packets


def ask_measurement(mode: str, index: int, total: int) -> float | None:
    """사람이 잰 값을 받는다. 빈 입력이면 그 시행을 버린다."""
    unit = "mm" if mode == "forward" else "도"
    what = "이동 거리" if mode == "forward" else "회전 각도"
    while True:
        raw = input(f"  [{index}/{total}] {what}({unit})? 엔터만 치면 이 시행을 버린다: ").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            print("    숫자를 입력한다")
            continue
        if value <= 0:
            print("    0 보다 커야 한다")
            continue
        return value


def summarize(trials: list[Trial], mode: str) -> float | None:
    """평균 비율을 낸다. **퍼짐이 크면 경고한다.**"""
    unit = "mm/s" if mode == "forward" else "도/s"
    if not trials:
        print("\n쓸 수 있는 시행이 없다.")
        return None
    print(f"\n{'시행':>4} {'요청':>7} {'실제창':>8} {'패킷':>5} {'측정':>9} {'비율':>10}")
    for t in trials:
        print(
            f"{t.index:>4} {t.requested_s:>6.2f}s {t.actual_s:>7.3f}s {t.packets:>5} "
            f"{t.measured:>9.1f} {t.rate:>7.1f} {unit}"
        )
    rates = [t.rate for t in trials]
    mean = statistics.fmean(rates)
    print(f"\n  평균 {mean:.1f} {unit}  (n={len(rates)})")
    if len(rates) >= 2:
        spread = statistics.stdev(rates)
        print(f"  표준편차 {spread:.1f} {unit} ({spread / mean * 100:.0f}%)")
        if spread > mean * SPREAD_WARN:
            print(
                f"  ⚠️ 퍼짐이 {SPREAD_WARN * 100:.0f}% 를 넘는다 — **바닥이 일정하지 않다.**\n"
                "     이 바닥에서는 회피 시퀀스의 후진·선회량을 신뢰할 수 없다.\n"
                "     시연할 바닥에서 다시 재거나 시행을 늘린다."
            )
    if len(rates) < 3:
        print("  ⚠️ WBS 2.2.3 은 **3회 이상 평균**을 요구한다.")
    return mean


def write_profile(
    device: str, mode: str, value: float, *, root: Path = Path("config/devices")
) -> Path | None:
    """개체 프로파일에 적는다. **`measured_on` 을 함께 적는다.**

    `root` 를 인자로 받는 것은 **시험 때문이다** — 실제 프로파일을 고치지 않고
    임시 사본으로 검증한다.

    ⚠️ `prod` 프로파일은 두 값과 `measured_on` 이 모두 있어야 기동한다
    (`config.py` 의 `validate_device_config`). 날짜 없이 숫자만 넣으면 *"언제 어느
    바닥에서 잰 값인가"* 를 잃고, 그러면 다시 재야 하는지 알 수 없다.
    """
    path = root / f"{device}.yaml"
    key = "forward_mm_per_sec" if mode == "forward" else "turn_deg_per_sec"
    text = path.read_text(encoding="utf-8")
    today = time.strftime("%Y-%m-%d")
    lines = text.splitlines(keepends=True)
    touched = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"{key}:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{key}: {value:.1f}\n"
            touched = True
        elif line.lstrip().startswith("measured_on:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f'{indent}measured_on: "{today}"\n'
    if not touched:
        print(f"⚠️ {path} 에서 `{key}:` 줄을 찾지 못했다 — 손으로 적는다", file=sys.stderr)
        return None
    path.write_text("".join(lines), encoding="utf-8")
    print(f"\n{path} 에 {key}: {value:.1f} · measured_on: {today} 를 적었다")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gait_calibrate", description="보행 이동량 실측")
    parser.add_argument("--device", default="mechdog-01", help="개체 id")
    parser.add_argument("--host", required=True, help="로봇 IP (목업이면 127.0.0.1)")
    parser.add_argument("--mode", choices=("forward", "turn"), required=True)
    parser.add_argument("--seconds", type=float, default=3.0, help="한 시행의 구동 시간")
    parser.add_argument("--trials", type=int, default=3, help="시행 횟수 (2.2.3 은 3회 이상)")
    parser.add_argument("--write", action="store_true", help="개체 프로파일에 결과를 적는다")
    parser.add_argument(
        "--dry-run", action="store_true", help="측정 입력 없이 송신 창만 확인한다 (목업 연습)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 실기 측정용
    # ⚠️ **가장 먼저 부른다.** cp949 콘솔에서 `⚠️` 가 있는 첫 `print` 가 죽는다.
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    config = load_config(args.device)
    gait, network = config["gait"], config["network"]
    step = float(gait["step_length_mm"])
    angle = float(gait["turn_angle_deg"]) if args.mode == "turn" else 0.0
    peer = (args.host, int(network["cmd_port"]))
    period_ms = 1000 // int(network["cmd_rate_hz"])

    print(f"개체 {args.device} · {peer[0]}:{peer[1]} · {args.mode}")
    print(f"명령 move({step:g}, {angle:g}) · 한 시행 {args.seconds:g}초 · {args.trials}회")
    if args.mode == "turn":
        print("⚠️ 제자리 회전은 불가하다 (ADR-11) — 원호로 돌므로 **시작·끝 방향**을 잰다")
    print("⚠️ 시연할 바닥에서 잰다. 카펫과 장판에서 값이 다르다.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=period_ms)
    sock.sendto(commander.open_session().encode("utf-8"), peer)
    # ⚠️ **로봇은 안전 상태로 깨어난다.** 래치가 걸린 채로는 `MOVE` 를 무시하므로
    # 측정 전에 한 번 풀어 준다 (PROTOCOL 2절). 목업도 기동 상태가 `FAILSAFE` 이며,
    # 이것을 빼고 돌리면 **패킷은 나가는데 로봇이 서 있다** — 그러면 이동거리 0 을
    # "쟀다" 로 적게 된다.
    sock.sendto(commander.clear_safe().encode("utf-8"), peer)
    time.sleep(0.2)

    trials: list[Trial] = []
    try:
        for index in range(1, args.trials + 1):
            if not args.dry_run:
                input(f"  [{index}/{args.trials}] 시작 위치를 표시하고 엔터")
            print(f"    {SETTLE_S:g}초 대기 후 {args.seconds:g}초 구동")
            time.sleep(SETTLE_S)
            actual, packets = drive_window(
                sock, peer, commander, step_mm=step, angle_deg=angle, seconds=args.seconds
            )
            print(f"    실제 송신 창 {actual:.3f}초 · 패킷 {packets}개")
            if args.dry_run:
                continue
            measured = ask_measurement(args.mode, index, args.trials)
            if measured is None:
                print("    버렸다")
                continue
            trials.append(Trial(index, args.seconds, actual, packets, measured))
    except KeyboardInterrupt:
        print("\n중단됐다")
    finally:
        sock.sendto(commander.emergency_stop().encode("utf-8"), peer)
        sock.close()

    if args.dry_run:
        print("\n연습 실행이었다 — 값을 내지 않는다")
        return 0
    mean = summarize(trials, args.mode)
    if mean is None:
        return 1
    if args.write:
        if len(trials) < 3:
            print("\n⚠️ 3회 미만이므로 적지 않는다 (WBS 2.2.3)", file=sys.stderr)
            return 1
        write_profile(args.device, args.mode, mean)
    else:
        key = "forward_mm_per_sec" if args.mode == "forward" else "turn_deg_per_sec"
        print(f"\n적으려면 --write. 손으로 적으려면 `{key}: {mean:.1f}`")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
