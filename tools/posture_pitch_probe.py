"""자세별 IMU 몸통 피치 실측 (WBS 2.1.4 ②).

로봇에 POSE 명령을 보내 자세를 바꾸면서 텔레메트리의 imu.pitch/roll 을
기록한다. 런타임이 명령 채널을 쓰고 있으면 같이 쓸 수 없으므로, 런타임을
내린 동안만 실행한다.

출력: 각 단계별 피치·롤 통계(중앙값/p95/최대) + 원본 샘플 JSON.
"""

import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host.common.protocol import CommandEncoder  # noqa: E402


def recv_telemetry(sock: socket.socket, deadline: float, keepalive=None) -> list[dict]:
    """deadline(epoch 초)까지 도착한 텔레메트리를 모은다.

    keepalive 가 주어지면 0.2초마다 호출한다 — 명령 타임아웃(~300ms)에 걸려
    링크 두절 FAILSAFE 로 들어가지 않게 하기 위한 하트비트다.
    """
    out = []
    sock.setblocking(False)
    next_ping = time.time()
    while time.time() < deadline:
        if keepalive is not None and time.time() >= next_ping:
            keepalive()
            next_ping = time.time() + 0.2
        try:
            data, _ = sock.recvfrom(65535)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(msg, dict) and isinstance(msg.get("imu"), dict):
            out.append({"t": time.time(), "imu": msg["imu"], "state": msg.get("state")})
    return out


def drain(sock: socket.socket) -> None:
    sock.setblocking(False)
    try:
        while True:
            sock.recvfrom(65535)
    except BlockingIOError:
        pass


def summarize(samples: list[dict], label: str) -> dict:
    # 부호 검증이 목적이므로 pitch 는 부호를 지우지 않는다 — abs() 를 걸면
    # "들었다/숙였다" 를 판정할 수 없다 (2026-09-15 실측: 명령 -15° → IMU 음수 = 앞이 올라감).
    pitches = sorted(s["imu"]["pitch"] for s in samples if "pitch" in s["imu"])
    rolls = sorted(abs(s["imu"]["roll"]) for s in samples if "roll" in s["imu"])

    def stats(vals):
        if not vals:
            return {"n": 0}
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        return {
            "n": len(vals),
            "median": round(vals[len(vals) // 2], 2),
            "p95": round(p95, 2),
            "max": round(vals[-1], 2),
        }

    return {"label": label, "pitch": stats(pitches), "roll": stats(rolls)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--cmd-port", type=int, default=5001)
    ap.add_argument("--telemetry-port", type=int, default=5101)
    ap.add_argument("--hold", type=float, default=4.0, help="단계별 유지·관측 초")
    ap.add_argument("--out", default=None, help="원본 JSON 경로")
    args = ap.parse_args()

    enc = CommandEncoder()
    cmd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tele = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tele.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tele.bind(("0.0.0.0", args.telemetry_port))

    def send(msg: str) -> None:
        cmd.sendto(msg.encode(), (args.host, args.cmd_port))
        print(f"  -> {msg}", flush=True)

    # 새 세션 규약: 첫 명령은 STOP seq=1 (최신 ts 로 순서 기준 초기화)
    send(enc.stop())
    drain(tele)
    time.sleep(0.5)
    # 링크 두절 FAILSAFE 래치 해제 — 런타임을 방금 내렸다면 걸려 있을 수 있다
    send(enc.reset_safe())
    time.sleep(1.0)

    results = []
    # 부호 실측 확정 (2026-09-15): 명령 pitch 음수 = 고개 들기(IMU 음수), 양수 = 숙이기.
    # 이 시험은 "고친 부호가 맞는가" 를 재확인한다 — 양수 하나를 끝에 두어 양쪽 방향을 본다.
    stages = [
        ("baseline-stand", None),  # 기본 자세 기준선
        ("pitch-10", {"pitch": -10, "roll": 0, "height": 0, "dur": 1500}),  # 들기
        ("pitch-20", {"pitch": -20, "roll": 0, "height": 0, "dur": 1500}),
        ("pitch-30", {"pitch": -30, "roll": 0, "height": 0, "dur": 1500}),
        ("pitch+15", {"pitch": 15, "roll": 0, "height": 0, "dur": 1500}),  # 숙이기 대조
        ("sit-low", {"pitch": 0, "roll": 0, "height": -25, "dur": 1500}),  # 앉기(낮춤) 후보
        ("restore", {"pitch": 0, "roll": 0, "height": 0, "dur": 1500}),
    ]
    for label, pose in stages:

        def keepalive(p=pose):
            send(enc.pose(**p) if p is not None else enc.stop())

        if pose is not None:
            keepalive()
        samples = recv_telemetry(tele, time.time() + args.hold, keepalive)
        s = summarize(samples, label)
        results.append(s)
        print(f"{label}: pitch={s['pitch']} roll={s['roll']} n={len(samples)}", flush=True)

    send(enc.stop())
    if args.out:
        Path(args.out).write_text(json.dumps({"stages": results}, indent=2, ensure_ascii=False))
        print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
