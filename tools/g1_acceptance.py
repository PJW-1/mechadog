"""G1 제어 링크 검수 (WBS 6.4.1 · M1).

    python tools/g1_acceptance.py --host 192.168.1.101 --serial COM9
    python tools/g1_acceptance.py --host 192.168.1.101 --serial COM9 --battery

검수 항목은 넷이다 — **조종 성공 · 송신 중단 후 300ms 내 정지 · 텔레메트리 10Hz ·
저전압 페일세이프**. 앞의 셋은 정식 빌드로 한 번에 돌리고, 저전압은 벤치 임계
빌드에서 `--battery` 로 따로 본다(가변전원 없이 보행 부하로 교차를 만든다).

⚠️ **기준기에서 수행한다** — 게이트 검수는 `phase1_reference` 개체에서만 인정된다
(CONTRIBUTING 1절). 다른 기체의 통과는 검수가 아니다.

⚠️ **로봇을 받침대에 올려 다리를 공중에 띄운다.** 이 도구는 실제로 걷게 한다.

⚠️ **`--battery` 의 시험용 경고 임계는 «쉬면 되올라오는 전압» 보다 위에 둔다.** 래치 뒤
기체가 멈추면 전압이 0.1V 남짓 회복되는데(2026-09-17 실측), 경고선이 그 회복 지점 아래면
감시기가 *"회복됐다"* 고 보고 재무장해 `RESET_SAFE` 거부가 풀린다. 실제 임계(7.0/6.6V)에서는
6.6V 에서 걸린 팩이 쉬어도 6.8V 라 생기지 않는 일이다 — **벤치 흉내의 한계이지 결함이 아니다.**
2026-09-17 검수에서는 경고 8.40V · 셧다운 8.05V 로 잡아 통과했다(정지 전압 8.25V).

⚠️ 관제 런타임(`host.runtime`)이 떠 있으면 명령 seq 가 경합하고 텔레메트리 포트도
겹친다. 검수 중에는 내려 둔다.
"""

from __future__ import annotations

import argparse
import re
import socket
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from obstacle_stop_probe import SerialLog  # noqa: E402
from service_action_probe import Tap, ack_for, pump  # noqa: E402

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402

#: 텔레메트리 주기 측정 창. 10Hz 면 300건이 나온다.
RATE_WINDOW_S = 30.0
#: 보행으로 «움직인다» 를 판정할 IMU 흔들림(도). 선 상태 노이즈는 0.1 안쪽이고
#: 트롯은 수 도씩 흔들린다.
MOTION_STDDEV_DEG = 0.4

#: 펌웨어가 타임아웃으로 스스로 세울 때 UART 에 남기는 줄.
_TIMEOUT_LINE = re.compile(r"FAILSAFE: command timeout")


def motion_stddev(rows: list[dict]) -> float:
    """구간의 IMU 흔들림. **판정을 소켓에서 떼어 둔다** (CONTRIBUTING 5.2)."""
    pitches = [r["imu"]["pitch"] for r in rows if isinstance(r.get("imu"), dict)]
    rolls = [r["imu"]["roll"] for r in rows if isinstance(r.get("imu"), dict)]
    if len(pitches) < 3:
        return 0.0
    return max(statistics.stdev(pitches), statistics.stdev(rolls))


def seq_rate(rows: list[dict], seconds: float) -> tuple[float, int]:
    """**새 `seq` 수**로 주기를 잰다 (건수가 아니다).

    ⚠️ 서버도 펌웨어도 마지막 값을 되풀이할 수 있다. 레코드 수로 세면 로봇이
    멈춰 있어도 10Hz 로 보인다 — `4.5.1`·`4.6.2` 에서 같은 판단을 했다.
    """
    seqs = {r["seq"] for r in rows if "seq" in r}
    return (len(seqs) / seconds if seconds > 0 else 0.0), len(seqs)


def main() -> int:
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description="G1 제어 링크 검수 (6.4.1)")
    ap.add_argument("--device", default="mechdog-01")
    ap.add_argument("--host", required=True, help="로봇 IP")
    ap.add_argument("--serial", default=None, help="UART 로그 포트 (예: COM9)")
    ap.add_argument(
        "--battery",
        action="store_true",
        help="⚠️ 벤치 임계 빌드에서만 — 저전압 래치와 RESET_SAFE 거부를 본다",
    )
    args = ap.parse_args()

    network = load_config(args.device)["network"]
    peer = (args.host, int(network["cmd_port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=100)
    tap = Tap(int(network["telemetry_port"]))
    log = SerialLog(args.serial)
    acks: list[dict] = []
    checks: list[tuple[bool, str]] = []

    try:
        sock.sendto(commander.open_session().encode("utf-8"), peer)
        commander.halt()
        pump(sock, peer, commander, 2.0, acks)
        log.drain()
        if not tap.rows:
            print("텔레메트리가 오지 않는다 — 주소·전원·다른 런타임 점유를 확인한다")
            return 2
        print(f"기준선: state={tap.field('state')} batt={tap.field('batt_v')}V")

        if args.battery:
            # ── ④ 저전압 페일세이프 (벤치 임계 빌드) ─────────────────────
            print("\n[④] 보행 부하로 셧다운선을 지나간다 — 걷기 시작")
            commander.once("RESET_SAFE")
            pump(sock, peer, commander, 1.5, acks)
            latched_before = tap.field("safety_latched")
            commander.drive(60.0, 0.0)
            for _ in range(8):
                pump(sock, peer, commander, 2.0, acks)
                print(
                    f"    batt={tap.field('batt_v'):.3f}V lowbatt={tap.field('lowbatt')} "
                    f"latched={tap.field('safety_latched')}"
                )
                if tap.field("safety_latched"):
                    break
            commander.halt()
            pump(sock, peer, commander, 0.5, acks)
            checks += [
                (latched_before is False, "시작 시점에는 래치가 없었다"),
                (tap.field("lowbatt") is True, "경고선을 지나 lowbatt 가 켜졌다"),
                (tap.field("safety_latched") is True, "셧다운선에서 SAFE 래치가 걸렸다"),
                (tap.field("state") == "FAILSAFE", "state 가 FAILSAFE 로 보고된다"),
            ]

            print("\n[④-2] 원인이 남아 있는 동안 RESET_SAFE 는 거부되어야 한다 (3.2.5)")
            acks.clear()
            commander.once("RESET_SAFE")
            pump(sock, peer, commander, 2.0, acks)
            refused = ack_for(acks, "RESET_SAFE")
            print(f"    RESET_SAFE ACK={refused}")
            checks += [
                (bool(refused) and refused.get("applied") is False, "RESET_SAFE 가 거부됐다"),
                (tap.field("safety_latched") is True, "거부 뒤에도 래치가 남아 있다"),
            ]
        else:
            # ── ① 조종 성공 ─────────────────────────────────────────────
            print("\n[①] 조종 — RESET_SAFE 뒤 전진 명령이 실제 보행으로 나가는가")
            commander.once("RESET_SAFE")
            pump(sock, peer, commander, 1.5, acks)
            acks.clear()
            mark = len(tap.rows)
            commander.drive(60.0, 0.0)
            pump(sock, peer, commander, 3.0, acks)
            walking = motion_stddev(tap.rows[mark:])
            move_ack = ack_for(acks, "MOVE")
            print(f"    MOVE ACK applied={move_ack.get('applied') if move_ack else None}")
            print(f"    보행 중 IMU 흔들림 {walking:.2f}° (기준 {MOTION_STDDEV_DEG}°)")
            checks += [
                (bool(move_ack) and move_ack.get("applied") is True, "MOVE 가 수락됐다"),
                (walking > MOTION_STDDEV_DEG, f"실제로 걸었다 (IMU {walking:.2f}°)"),
            ]

            # ── ② 송신 중단 후 300ms 내 정지 ────────────────────────────
            print("\n[②] 송신 중단 — STOP 도 보내지 않는다. 펌웨어가 스스로 서야 한다")
            log.drain()
            log.text = ""
            silence_started = time.perf_counter()
            time.sleep(1.5)  # 침묵. 명령을 한 건도 보내지 않는다
            log.drain()
            quiet_rows = [r for r in tap.rows if r["_at"] >= silence_started + 0.5]
            quiet = motion_stddev(quiet_rows)
            latched = tap.field("safety_latched")
            print(f"    침묵 뒤 IMU 흔들림 {quiet:.2f}° · latched={latched}")
            timeout_logged = bool(_TIMEOUT_LINE.search(log.text))
            if args.serial:
                print(f"    UART 타임아웃 기록: {'있음' if timeout_logged else '없음'}")
            checks += [
                (quiet <= MOTION_STDDEV_DEG, f"다리가 섰다 (IMU {quiet:.2f}°)"),
                (latched is True, "300ms 타임아웃으로 SAFE 래치가 걸렸다"),
            ]

            # ── ③ 텔레메트리 10Hz ───────────────────────────────────────
            print(f"\n[③] 텔레메트리 주기 — {RATE_WINDOW_S:.0f}초 동안 새 seq 를 센다")
            commander.halt()
            mark = len(tap.rows)
            started = time.perf_counter()
            pump(sock, peer, commander, RATE_WINDOW_S, acks)
            elapsed = time.perf_counter() - started
            rate, unique = seq_rate(tap.rows[mark:], elapsed)
            print(f"    새 seq {unique}건 / {elapsed:.1f}초 = {rate:.2f} Hz")
            checks.append((rate >= 9.5, f"텔레메트리 10Hz ({rate:.2f} Hz)"))
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
