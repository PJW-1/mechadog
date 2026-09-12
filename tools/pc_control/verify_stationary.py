"""Bounded Wi-Fi STOP/telemetry check of the installed stationary image. No serial."""

import argparse
import json
import select
import socket
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mode_control import exclusive_operation
from ota_update import RobotOta
from settings import SETTINGS, require_settings
from telemetry_probe import ProbeStats

from host.common.protocol import CommandEncoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    args = parser.parse_args()
    require_settings()
    root = args.output.resolve()
    repo = Path(__file__).resolve().parents[2]
    if root.is_relative_to(repo):
        parser.error("Measurement outputs must be outside the repository")
    root.mkdir(parents=True, exist_ok=False)
    config = SETTINGS.client()
    package = json.loads(SETTINGS.packages[0].read_text(encoding="utf-8-sig"))
    stats = ProbeStats("mechdog-" + config["mac"])
    encoder = CommandEncoder()
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "commands": ["STOP"],
        "serial_used": False,
    }
    with (root / "stationary-wifi-verification.json").open("x", encoding="utf-8") as summary:
        try:
            with exclusive_operation(), ExitStack() as stack:
                client = RobotOta(config)
                before = client.status()
                assert (
                    before["healthy"]
                    and before["confirmed"]
                    and before["image_sha256"] == package["image_sha256"]
                )
                report["before"] = before
                command = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                command.connect((config["host"], 5001))
                local_ip = command.getsockname()[0]
                receiver = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                receiver.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                receiver.bind((local_ip, 5101))
                log = stack.enter_context(
                    (root / "stationary-wifi-raw.jsonl").open("x", encoding="utf-8")
                )

                def record(item):
                    log.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    log.flush()

                began = time.monotonic()
                next_send = began
                sent = {}
                rtts = []
                timestamps = []
                batteries = []
                expired_link = 0
                acks = 0
                while time.monotonic() - began < 35:
                    now = time.monotonic()
                    if now - began < 30 and now >= next_send:
                        payload = encoder.stop()
                        msg = json.loads(payload)
                        command.send(payload.encode())
                        sent[msg["seq"]] = now
                        record({"kind": "sent", "at": now, "payload": msg})
                        next_send = now + 0.1
                    for sock in select.select([command, receiver], [], [], 0.02)[0]:
                        raw, peer = sock.recvfrom(65535)
                        received = time.monotonic()
                        if peer[0] != config["host"]:
                            continue
                        if sock is command:
                            msg = json.loads(raw)
                            assert msg["type"] == "STOP" and msg["seq"] in sent
                            assert (
                                msg.get("ok") is True
                                and msg.get("actuators") is False
                                and msg.get("safe_latched") is True
                            )
                            acks += 1
                            rtts.append((received - sent[msg["seq"]]) * 1000)
                            record({"kind": "ack", "at": received, "payload": msg})
                        else:
                            item = stats.ingest(raw, received)
                            record(item)
                            if item["kind"] == "telemetry":
                                msg = item["telemetry"]
                                assert (
                                    msg["state"] == "FAILSAFE" and msg.get("safety_latched") is True
                                )
                                timestamps.append(time.time_ns() // 1000000 - msg["ts"])
                                batteries.append(msg["batt_v"])
                                if received - began > 32 and msg["flags"]["link_ok"] is False:
                                    expired_link += 1
                ended = time.monotonic()
                report.update(stats.summary(began, ended))
                report.update(
                    sent_commands=len(sent),
                    ack_count=acks,
                    ack_rtt_max_ms=max(rtts, default=None),
                    command_anchored_timestamp_age_max_ms=max(timestamps, default=None),
                    battery_min_v=min(batteries, default=None),
                    battery_max_v=max(batteries, default=None),
                    after_command_stop_link_false_packets=expired_link,
                )
                report["after"] = client.status()
                assert (
                    report["after"]["boot"] == before["boot"]
                    and report["after"]["healthy"]
                    and report["after"]["confirmed"]
                )
                assert report["after"]["image_sha256"] == package["image_sha256"]
                assert (
                    report["success"]
                    and acks == len(sent)
                    and expired_link > 0
                    and len(stats.sessions) == 1
                )
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((local_ip, 5101))
            report["host_receive_port_released"] = True
            report["state"] = "PASS"
        except Exception as exc:
            report.update(state="FAILED", error_type=type(exc).__name__, error=str(exc))
        finally:
            report["finished_at"] = datetime.now(UTC).isoformat()
            json.dump(report, summary, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False), flush=True)

    return 0 if report["state"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
