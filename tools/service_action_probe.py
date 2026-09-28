"""SERVICE 모드에서 ACTION 이 거부되고 보드가 재부팅하지 않는지 확인한다.

    python tools/service_action_probe.py --host 192.168.1.101
    python tools/service_action_probe.py --host 192.168.1.101 --regression

**왜 필요한가.** SERVICE 는 구동 빌드에서 **루프 워치독이 무장되는 유일한 모드**다
(`enterServiceMode`, 마감 750ms). 그런데 벤더 `action_run` 은 1,003~1,006ms 를
blocking 한다(`motion_hal.cpp`). 둘이 만나면 정비하려고 세워 둔 기체가 다리를
움직이다 재부팅한다 — 사람 손이 올라가 있는 상황이다. 그래서 `can_action()` 이
`service_mode` 를 막는다. 이 도구는 그 가드가 실기에서 실제로 서 있는지 본다.

**판정 기준은 `boot_id` 다.** 펌웨어가 부팅할 때마다 새로 만들어 모든 텔레메트리에
싣는다. ACTION 전후로 같으면 재부팅하지 않은 것이다 — 눈으로 추측하지 않는다.

⚠️ `--regression` 없이는 **로봇이 움직이지 않는다.** SERVICE 중 ACTION 은 거부되고
나머지는 전부 STOP 이다. `--regression` 을 주면 SERVICE 를 나와 ACTION 을 한 번
실제로 실행해 *정상 경로를 막지 않았는지* 본다 — 그때는 기체가 크게 움직인다.

⚠️ 관제 런타임(`host.runtime`)이 떠 있으면 seq 가 경합하고 텔레메트리 포트도
겹친다. 측정 중에는 내려 둔다.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402

#: SERVICE 중 보낼 ACTION. 거부될 것이므로 무엇이든 되지만, 만에 하나 실행되더라도
#: 이미 서 있는 기체에 가장 무해한 것을 고른다 (`motion_hal.cpp` 의 id 표).
DEFAULT_ACTION_ID = 0  # stand_four_legs
#: ACTION 뒤 관찰 창. 워치독 재부팅은 약 813ms 에 나므로(WATCHDOG_VERIFICATION 7절)
#: 6초면 재부팅과 Wi-Fi 재접속까지 충분히 드러난다.
OBSERVE_S = 6.0


class Tap:
    """텔레메트리 수신기. 판정에 쓰는 것은 `boot_id` 와 `flags.service` 뿐이다."""

    def __init__(self, port: int) -> None:
        self.rows: list[dict] = []
        self.boot_ids: list[str] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR 를 두지 않는다 — 포트를 누가 잡고 있으면 조용히 표본 0 개가
        # 되는 대신 bind 에서 바로 터진다 (PR #162 와 같은 이유).
        self._sock.bind(("0.0.0.0", port))
        self._sock.settimeout(0.2)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            try:
                self.ingest(json.loads(raw), time.perf_counter())
            except ValueError:
                continue

    def ingest(self, msg: dict, at: float) -> None:
        """레코드 하나를 받아들인다. **소켓과 분리해 둔 이유는 판정이 여기 있기
        때문이다** — 재부팅 횟수를 세는 것이 이 도구의 전부다 (CONTRIBUTING 5.2
        HAL 분리)."""
        msg["_at"] = at
        self.rows.append(msg)
        boot = msg.get("boot_id")
        # 같은 값이 이어지는 동안은 한 번만 센다. 길이가 곧 부팅 횟수다.
        if boot and (not self.boot_ids or self.boot_ids[-1] != boot):
            self.boot_ids.append(boot)

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def field(self, name: str):
        """최신 레코드의 필드. `flags` 안쪽도 같이 본다."""
        if not self.rows:
            return None
        row = self.rows[-1]
        return row.get(name, (row.get("flags") or {}).get(name))

    def since(self, at: float) -> list[dict]:
        return [r for r in self.rows if r["_at"] >= at]


def pump(sock, peer, commander: Commander, seconds: float, acks: list[dict]) -> None:
    """`seconds` 동안 10Hz 로 현재 의도를 보내며 ACK 를 모은다.

    ⚠️ **침묵하면 로봇이 잠긴다.** 명령이 300ms 끊기면 스스로 페일세이프 래치를
    건다. 그래서 "가만히 있는 구간" 도 STOP 을 계속 보내는 구간이어야 한다.
    """
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        for packet in commander.tick(system_clock_ms()):
            sock.sendto(packet.encode("utf-8"), peer)
        try:
            while True:
                acks.append(json.loads(sock.recvfrom(2048)[0]))
        except (BlockingIOError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(0.01)


def ack_for(acks: list[dict], type_: str) -> dict | None:
    """그 명령 **자체의** ACK. ⚠️ `applied` 만 세면 STOP ACK 가 섞여 늘 통과한다."""
    hits = [a for a in acks if a.get("type") == type_]
    return hits[0] if hits else None


def main() -> int:
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description="SERVICE 중 ACTION 가드 실기 확인")
    ap.add_argument("--device", default="mechdog-01")
    ap.add_argument("--host", required=True, help="로봇 IP")
    ap.add_argument("--action-id", type=int, default=DEFAULT_ACTION_ID)
    ap.add_argument(
        "--regression", action="store_true", help="⚠️ SERVICE 밖 ACTION 도 실행 — 기체가 움직인다"
    )
    args = ap.parse_args()

    network = load_config(args.device)["network"]
    peer = (args.host, int(network["cmd_port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=100)
    tap = Tap(int(network["telemetry_port"]))
    acks: list[dict] = []
    checks: list[tuple[bool, str]] = []

    try:
        sock.sendto(commander.open_session().encode("utf-8"), peer)
        commander.halt()
        pump(sock, peer, commander, 2.0, acks)
        if not tap.rows:
            print("텔레메트리가 오지 않는다 — 주소·전원·다른 런타임 점유를 확인한다")
            return 2
        print(
            f"boot_id={tap.field('boot_id')} state={tap.field('state')} batt={tap.field('batt_v')}"
        )

        print("\n[1] SERVICE 진입")
        acks.clear()
        commander.once("SERVICE", mode="enter")
        pump(sock, peer, commander, 3.0, acks)
        entered = ack_for(acks, "SERVICE")
        if tap.field("service") is not True:
            print(f"  SERVICE 에 못 들어갔다 — 이 빌드에 없거나 거부됐다. ACK={entered}")
            return 3
        armed = bool(entered and entered.get("wdt_armed"))
        boot_before = tap.field("boot_id")
        print(f"  service=True wdt_armed={armed} boot_id={boot_before}")
        checks.append((armed, "워치독이 무장됐다 (= 위험 조건이 실제로 성립)"))

        print(f"\n[2] SERVICE 중 ACTION {args.action_id} — 거부되고 재부팅하지 않아야 한다")
        mark = time.perf_counter()
        acks.clear()
        commander.once("ACTION", id=args.action_id)
        pump(sock, peer, commander, OBSERVE_S, acks)
        blocked = ack_for(acks, "ACTION")
        rows = tap.since(mark)
        print(f"  ACTION ACK={blocked}")
        print(f"  텔레메트리 {len(rows)}건 · boot_id {boot_before} → {tap.field('boot_id')}")
        checks += [
            (bool(blocked) and blocked.get("applied") is False, "ACTION 이 거부됐다"),
            (tap.field("boot_id") == boot_before, "boot_id 불변 (= 재부팅하지 않았다)"),
            (len(rows) >= OBSERVE_S * 5, f"텔레메트리가 끊기지 않았다 ({len(rows)}건)"),
            (tap.field("service") is True, "SERVICE 가 유지됐다"),
        ]

        print("\n[3] SERVICE 이탈")
        acks.clear()
        commander.once("SERVICE", mode="exit")
        pump(sock, peer, commander, 3.0, acks)
        left = ack_for(acks, "SERVICE")
        print(f"  ACK={left}")
        checks.append((bool(left) and left.get("service_mode") is False, "SERVICE 를 빠져나왔다"))

        if args.regression:
            print(f"\n[4] ⚠️ SERVICE 밖 ACTION {args.action_id} — 기체가 실제로 움직인다")
            commander.once("RESET_SAFE")
            pump(sock, peer, commander, 1.5, acks)
            acks.clear()
            boot_pre = tap.field("boot_id")
            commander.once("ACTION", id=args.action_id)
            pump(sock, peer, commander, OBSERVE_S, acks)
            ran = ack_for(acks, "ACTION")
            print(f"  ACTION ACK={ran}")
            checks += [
                (bool(ran) and ran.get("applied") is True, "SERVICE 밖에서는 ACTION 이 동작한다"),
                (tap.field("boot_id") == boot_pre, "정상 ACTION 으로도 재부팅하지 않는다"),
            ]

        commander.halt()
        pump(sock, peer, commander, 1.0, acks)

        print("\n" + "=" * 58)
        for ok, label in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        passed = all(ok for ok, _ in checks)
        print(f"  판정: {'통과' if passed else '실패'}  (재부팅 {len(tap.boot_ids) - 1}회)")
        print("=" * 58)
        return 0 if passed else 1
    finally:
        tap.close()
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
