"""Send timestamped JSON commands to the MechDog ESP32 over UDP."""

from __future__ import annotations

import argparse
import contextlib
import json
import socket
import sys
import time


class Client:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.target = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            with contextlib.suppress(OSError):
                self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        self.timeout = timeout
        self.seq = 0

    def close(self) -> None:
        self.sock.close()

    def encode(self, command_type: str, **fields: object) -> tuple[int, bytes]:
        self.seq += 1
        message = {
            "seq": self.seq,
            "ts": int(time.time() * 1000),
            "type": command_type,
            **fields,
        }
        return self.seq, json.dumps(message, separators=(",", ":")).encode()

    def send_only(self, command_type: str, **fields: object) -> int:
        seq, raw = self.encode(command_type, **fields)
        self.sock.sendto(raw, self.target)
        return seq

    def receive(self, expected_seq: int) -> dict[str, object]:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("응답 대기 시간 초과")
            self.sock.settimeout(max(0.001, remaining))
            reply, source = self.sock.recvfrom(2048)
            decoded = json.loads(reply)
            if decoded.get("seq") != expected_seq:
                continue
            print(f"{source[0]}:{source[1]} {decoded}")
            return decoded

    def send(self, command_type: str, **fields: object) -> dict[str, object]:
        return self.receive(self.send_only(command_type, **fields))

    def send_raw(self, raw: bytes) -> dict[str, object]:
        self.sock.sendto(raw, self.target)
        reply, source = self.sock.recvfrom(2048)
        decoded = json.loads(reply)
        print(f"{source[0]}:{source[1]} {decoded}")
        return decoded


def establish_safe_session(client: Client) -> None:
    client.send("STOP")
    # Do not wait for RESET_SAFE's ACK. Waiting would couple the 10 Hz command
    # stream to Wi-Fi RTT and can trigger the 300 ms watchdog by itself.
    client.send_only("RESET_SAFE")


def run_safety(client: Client) -> int:
    client.send("STOP")
    malformed = client.send_raw(b'{"seq":2,"type":"MOVE"')
    if malformed.get("ok") is not False:
        raise RuntimeError("malformed JSON was not rejected")
    client.send("RESET_SAFE")
    client.send("ESTOP")
    blocked = client.send("MOVE", step=20, angle=0)
    if blocked.get("applied") is not False or blocked.get("safe_latched") is not True:
        raise RuntimeError("MOVE was not blocked by ESTOP latch")
    client.send("RESET_SAFE")
    client.send("STOP")
    print("SAFETY RESULT: malformed=discarded ESTOP=latched MOVE=blocked reset=accepted")
    return 0


def run_move(client: Client, step: float, angle: float, duration: float) -> int:
    establish_safe_session(client)
    next_send = time.monotonic()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        client.send_only("MOVE", step=step, angle=angle)
        next_send += 0.1
        time.sleep(max(0.0, next_send - time.monotonic()))
    client.send("STOP")
    print("MOVE RESULT: command stream ended with STOP")
    return 0


def run_watchdog(client: Client, step: float, angle: float, duration: float) -> int:
    initial = client.send("STOP")
    last_count = int(initial.get("failsafe_count", 0))
    client.send_only("RESET_SAFE")
    next_send = time.monotonic()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        client.send_only("MOVE", step=step, angle=angle)
        next_send += 0.1
        time.sleep(max(0.0, next_send - time.monotonic()))

    print("Intentionally stopping command transmission for 0.45 s...")
    time.sleep(0.45)
    state = client.send("STOP")
    if state.get("safe_latched") is not True:
        raise RuntimeError("300 ms command watchdog did not latch SAFE")
    if int(state.get("failsafe_count", 0)) <= last_count:
        raise RuntimeError("failsafe counter did not advance")
    print("WATCHDOG RESULT: SAFE latched after command stream stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--timeout", type=float, default=1.0)
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("safety", help="verify malformed-command and ESTOP behavior")
    move = sub.add_parser("move", help="send MOVE at 10 Hz and finish with STOP")
    watchdog = sub.add_parser("watchdog", help="stop sending MOVE and verify 300 ms failsafe")
    for command in (move, watchdog):
        command.add_argument("--step", type=float, default=20.0)
        command.add_argument("--angle", type=float, default=0.0)
        command.add_argument("--duration", type=float, default=0.5)

    args = parser.parse_args()
    client = Client(args.host, args.port, args.timeout)
    try:
        try:
            if args.action == "safety":
                return run_safety(client)
            if args.action == "move":
                return run_move(client, args.step, args.angle, args.duration)
            return run_watchdog(client, args.step, args.angle, args.duration)
        except OSError as exc:
            print(f"통신 실패: {exc}", file=sys.stderr)
            return 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
