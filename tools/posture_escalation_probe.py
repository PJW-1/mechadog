"""자세 상승(`PostureEscalation`)을 실물에 물려 확인한다 (WBS 3.5.7 · FR-9.2).

`host/behavior/posture.py` 는 «무엇을 할지» 만 정하고 전송은 하지 않는다. 그 소비처가
아직 런타임에 없어서 **모듈은 단위시험으로만 닫혀 있었다.** 이 도구가 그 소비처를
임시로 맡아, 카메라 → 클리핑 판정 → 명령 전송 → 재관측 고리를 실기에서 한 번 돈다.

    python tools/posture_escalation_probe.py --host 192.168.1.103 \
        --camera-ip 192.168.1.102 --seconds 40 --out-dir <경로>

⚠️ **런타임을 먼저 내린다.** 텔레메트리 포트(5101)를 둘이 잡을 수 없다.
⚠️ **대상은 0.6m 쯤에 «서» 있어야 한다.** 머리가 잘려야 개시하고(FR-9.2.1),
   움직이면 개시하지 않는다(FR-9.2.0 — `static_frames` 만큼 조용해야 한다).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host.behavior.posture import (  # noqa: E402
    RETURN,
    STEP_BACK_OFF,
    STEP_PITCH_UP,
    STEP_SIT,
    PostureEscalation,  # noqa: E402
)
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder  # noqa: E402
from host.vision.coco_labels import COCO_CLASSES  # noqa: E402
from host.vision.detector import Detector  # noqa: E402
from host.vision.stream_client import (  # noqa: E402
    StreamEndpoints,
    StreamReader,
    apply_profile,
)
from host.vision.worker import VisionWorker  # noqa: E402
from tools.posture_pitch_probe import KEEPALIVE_S, drain  # noqa: E402

#: 대상은 한 명이라고 본다. `PostureEscalation` 은 ID 가 바뀌면 재시도 횟수를 새로
#: 세는데, 이 도구는 추적기를 물리지 않으므로 그 재설정이 일어나면 안 된다.
TRACK_ID = 0


def largest_person(detections) -> tuple[float, float, float, float] | None:
    """가장 큰 `person` 박스. 여러 명이면 제일 가까운 사람이 제일 크다."""
    people = [d for d in detections if d.label == "person"]
    if not people:
        return None
    return max(people, key=lambda d: (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])).box


def main() -> int:
    survive_encoding_errors()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="mechdog-01")
    ap.add_argument("--host", required=True, help="로봇 IP")
    ap.add_argument("--camera-ip", required=True)
    ap.add_argument("--cmd-port", type=int, default=5001)
    ap.add_argument("--telemetry-port", type=int, default=5101)
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument(
        "--back-off-s",
        type=float,
        default=2.0,
        help="3단계 후진을 이어 보낼 초. 보폭 실측이 없어 명목값이다",
    )
    ap.add_argument(
        "--pitch-up-deg",
        type=float,
        default=None,
        help=(
            "1단계 각도를 설정 대신 이 값으로. **사다리 자체를 시험할 때만 쓴다** — "
            "일부러 약하게 주면 1단계가 못 풀어 2·3단계로 올라간다"
        ),
    )
    ap.add_argument(
        "--frame-every-s",
        type=float,
        default=2.0,
        help="증거용으로 프레임을 남길 주기. 결정이 안 나도 «무엇을 보고 있었나» 가 남는다",
    )
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()

    config = load_config(args.device)
    esc = PostureEscalation(config)
    step_mm = int(config["gait"]["step_length_mm"])
    pitch_up_deg = (
        float(config["posture"]["pitch_up_deg"])
        if args.pitch_up_deg is None
        else float(args.pitch_up_deg)
    )
    settle_ms = int(config["posture"]["settle_ms"])
    return_first = bool(config["posture"]["return_before_move"])

    args.out_dir.mkdir(parents=True, exist_ok=False)
    enc = CommandEncoder()
    # `posture_pitch_probe` 와 같은 이유로 소켓 하나를 5101 에 묶는다 (SO_REUSEADDR 없이).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.telemetry_port))
    sock.setblocking(False)
    target = (str(ipaddress.IPv4Address(args.host)), args.cmd_port)

    def send(msg: str) -> None:
        sock.sendto(msg.encode(), target)

    address = ipaddress.IPv4Address(args.camera_ip)
    endpoints = StreamEndpoints(control=f"http://{address}", stream=f"http://{address}:81/stream")
    # ⚠️ **장착 방향을 내려보내지 않으면 카메라가 뒤집힌 영상을 보낸다.** `/orient` 가
    # 바꾸는 것은 XIAO 의 전역 변수라 **전원을 껐다 켜면 0 으로 돌아간다.** 런타임은
    # 기동 때 이것을 부르지만 이 도구는 런타임 없이 돌므로 스스로 불러야 한다 —
    # 2026-09-20 실기에서 빼먹어 899 프레임 중 사람 검출이 **0** 이었다(사람이 거꾸로
    # 서 있으니 모델이 못 본다).
    profile = apply_profile(config, endpoints=endpoints)
    print(f"camera profile={profile.get('profile')} rot={config['vision']['mount_rotation']}")
    worker = VisionWorker(
        config,
        detector=Detector(config, labels=COCO_CLASSES),
        reader=StreamReader(config, endpoints=endpoints),
    )

    rows: list[dict] = []
    events: list[dict] = []
    previous = None
    #: 후진 구간이면 하트비트가 STOP 이 아니라 MOVE 다. 그 끝나는 시각.
    back_off_until = 0.0
    #: 다음 정기 프레임을 남길 시각.
    next_frame_at = 0.0
    error = None
    worker.start()
    try:
        send(enc.stop())
        drain(sock)
        # ⚠️ 여기서 쉬지 않는다 — 300ms 침묵이면 로봇이 다시 래치한다.
        send(enc.reset_safe())
        start = time.monotonic()
        last_ping = 0.0
        deadline = start + args.seconds
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_ping >= KEEPALIVE_S:
                send(enc.move(step=-step_mm, angle=0) if now < back_off_until else enc.stop())
                last_ping = now
            drain(sock)

            result = worker.latest()
            if result is None or result is previous:
                time.sleep(0.005)
                continue
            previous = result
            box = largest_person(result.detections)
            now_ms = int((now - start) * 1000)
            decision = esc.update(
                box=box, frame_height=result.frame_height, track_id=TRACK_ID, now_ms=now_ms
            )
            row = {
                "t_ms": now_ms,
                "frame_seq": result.frame_seq,
                "y1": None if box is None else round(box[1], 1),
                "y2": None if box is None else round(box[3], 1),
                "held": esc.step,
                "retries": esc.retries,
                "step": decision.step,
                "reason": decision.reason,
                "undetermined": decision.undetermined,
            }
            rows.append(row)

            if now >= next_frame_at:
                (args.out_dir / f"t{now_ms:06d}.jpg").write_bytes(result.jpeg)
                next_frame_at = now + args.frame_every_s

            if decision.step is None:
                continue
            # ── 결정을 명령으로 옮긴다. 자세 명령은 **한 번만** 보낸다.
            if decision.step == STEP_PITCH_UP:
                send(enc.pose(pitch=pitch_up_deg, roll=0, height=0, dur=settle_ms))
            elif decision.step == STEP_SIT:
                send(enc.encode("ACTION", id=1))
            elif decision.step == STEP_BACK_OFF:
                if return_first:  # FR-9.2.3 — 움직이기 전에 기본 자세로
                    send(enc.encode("ACTION", id=0))
                back_off_until = time.monotonic() + args.back_off_s
            elif decision.step == RETURN:
                send(enc.encode("ACTION", id=0))
            (args.out_dir / f"{len(events):02d}_{decision.step}.jpg").write_bytes(result.jpeg)
            events.append(row)
            print(
                f"[{now_ms:6d}ms] y1={row['y1']} -> {decision.step} ({decision.reason})", flush=True
            )
    except Exception as exc:  # noqa: BLE001 — 부분 기록이라도 남긴다
        error = f"{type(exc).__name__}: {exc}"
    finally:
        worker.stop()
        send(enc.encode("ACTION", id=0))  # 기본 자세로 되돌린다
        send(enc.stop())
        sock.close()

    (args.out_dir / "frames.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    summary = {
        "frames": len(rows),
        "frames_with_person": sum(1 for r in rows if r["y1"] is not None),
        "clipped_frames": sum(1 for r in rows if r["y1"] is not None and r["y1"] <= 8),
        "decisions": [e["step"] for e in events],
        "undetermined": any(r["undetermined"] for r in rows),
        "final_retries": esc.retries,
        "error": error,
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if (error or not summary["frames_with_person"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
