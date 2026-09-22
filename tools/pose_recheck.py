"""단일 자세 재측정 — posture 경로와 동일한 명령 시퀀스로 pitch 하나만 보낸다.

사용: python tools/pose_recheck.py <pitch_deg> [hold_s]
  POSE -> 4s 유지 표본 -> hold_s 동안 HALT 펌프로 자세 유지 -> POSE 0 복귀 -> ESTOP
"""

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import field_measure as fm

DEVICE = "mechdog-02"
HOST = "192.168.0.18"


def main() -> int:
    angle = float(sys.argv[1]) if len(sys.argv) > 1 else -10.0
    hold = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
    config = fm.load_config(DEVICE)
    net = config["network"]
    tap = fm.TelemetryTap(int(net["telemetry_port"]), config["telemetry_device_id"], HOST)
    ok, _ = tap.wait_flag(lambda _r: True, timeout_s=5)
    if not ok:
        print("[실패] 해당 기체의 텔레메트리 표본이 없습니다. 명령을 보내지 않았습니다.")
        return 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    cmd = fm.Commander(
        fm.CommandEncoder(clock=fm.system_clock_ms),
        period_ms=int(1000 / net.get("cmd_rate_hz", 10)),
    )
    peer = (HOST, int(net["cmd_port"]))
    try:
        sock.sendto(cmd.open_session().encode(), peer)
        cmd.once("RESET_SAFE")
        cmd.halt()
        fm.pump(sock, peer, cmd, 0.5, fm.Acks())
        cmd.once("POSE", pitch=angle, roll=0, height=0, dur=1500)
        start = time.perf_counter()
        fm.pump(sock, peer, cmd, 4.0, fm.Acks())
        samples = [s for s in tap.window(2) if s.at >= start + 2]
        pitches = [s.reading.pitch for s in samples if s.reading.pitch is not None]
        if pitches:
            print(f"[IMU] pitch mean {sum(pitches) / len(pitches):+.2f} deg (n={len(pitches)})")
        print(f"[유지] {hold:.0f}s 동안 자세 유지 — 지금 폰으로 측정하세요")
        end = time.perf_counter() + hold
        while time.perf_counter() < end:
            cmd.halt()
            fm.pump(sock, peer, cmd, 0.2, fm.Acks())
            if tap.latest is not None:
                print(f"  live pitch {tap.latest.pitch:+.2f}")
    finally:
        try:
            cmd.once("POSE", pitch=0, roll=0, height=0, dur=1500)
            fm.pump(sock, peer, cmd, 2.0, fm.Acks())
            sock.sendto(cmd.emergency_stop().encode(), peer)
        finally:
            tap.close()
            sock.close()
    print("[완료] 중립 복귀 + ESTOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
