"""Send UDP probes to a MechDog and verify its echo responses."""

from __future__ import annotations

import argparse
import socket
import statistics
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="MechDog ESP32 IPv4 address")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.1)
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.interval < 0:
        parser.error("--interval must be zero or greater")

    latencies_ms: list[float] = []
    failures: list[str] = []
    stale_replies = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for index in range(1, args.count + 1):
            payload = f"PING {index} {time.time_ns()}".encode()
            expected = b"ACK " + payload
            started = time.perf_counter()
            deadline = started + args.timeout
            sock.sendto(payload, (args.host, args.port))
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    failures.append(f"#{index}: timeout")
                    break
                sock.settimeout(remaining)
                try:
                    reply, source = sock.recvfrom(1024)
                except TimeoutError:
                    failures.append(f"#{index}: timeout")
                    break
                elapsed_ms = (time.perf_counter() - started) * 1000
                if reply == expected:
                    latencies_ms.append(elapsed_ms)
                    break
                stale_replies += 1

            if index < args.count and args.interval:
                time.sleep(args.interval)

    received = len(latencies_ms)
    loss_percent = (len(failures) / args.count) * 100
    if received:
        print(
            f"RESULT: sent={args.count} received={received} "
            f"lost={len(failures)} loss={loss_percent:.1f}% "
            f"rtt_avg={statistics.fmean(latencies_ms):.1f}ms "
            f"rtt_min={min(latencies_ms):.1f}ms "
            f"rtt_max={max(latencies_ms):.1f}ms "
            f"stale={stale_replies}"
        )
    else:
        print(
            f"RESULT: sent={args.count} received=0 "
            f"lost={len(failures)} loss=100.0% stale={stale_replies}"
        )

    for failure in failures[:10]:
        print(f"FAIL: {failure}")
    if len(failures) > 10:
        print(f"FAIL: ... and {len(failures) - 10} more")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
