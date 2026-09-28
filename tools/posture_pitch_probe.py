"""자세별 IMU 몸통 피치 실측 (WBS 2.1.4 ②).

로봇에 자세 명령을 보내면서 텔레메트리의 imu.pitch/roll 을 기록한다. 런타임이 명령
채널을 쓰고 있으면 같이 쓸 수 없으므로, 런타임을 내린 동안만 실행한다.

출력: 각 단계별 피치·롤 통계(중앙값/최소/최대) + 원본 샘플 JSON.

⚠️ **부호를 버리지 않는다.** 이 측정의 요점이 «몸통이 어느 쪽으로 기우는가» 이기
때문이다. 2026-09-15 실기에서 `config` 의 `alert_pitch_up_deg: +15` 가 이름과 반대로
**앞이 내려가는** 값이었다는 것이 드러났는데, 절대값으로 재면 그런 오류를 영원히 볼 수
없다. 명령 부호와 `imu.pitch` 부호는 같은 방향이다 — **음수가 «고개를 드는» 쪽**이다
(PROTOCOL `POSE` 부호표).
"""

import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder  # noqa: E402

#: 명령 하트비트 주기. 온보드 명령 타임아웃이 300ms 이고 규약이 10Hz 고정 송신이다.
KEEPALIVE_S = 0.1

#: 이보다 적은 표본의 중앙값은 쓰지 않는다. 텔레메트리는 10Hz 가 정상이라 기본
#: 관측 3초면 약 30개가 와야 한다. 크게 모자라면 로봇이 텔레메트리를 억제하고 있다는
#: 뜻이다 — 예컨대 배터리가 `[6.0, 8.6]` 밖이면 펌웨어가 발행을 멈춘다(UART 에
#: `Telemetry unavailable: battery outside` 가 찍힌다). 만충 직후가 그 경우다.
MIN_SAMPLES = 10


def drain(sock: socket.socket) -> None:
    """읽지 않은 전문을 버린다.

    ⚠️ **`ConnectionResetError` 도 함께 삼킨다.** Windows 는 아직 아무도 듣지 않는
    포트로 보낸 뒤 돌아온 ICMP Port Unreachable 을 **다음 `recvfrom` 의**
    `ConnectionResetError` 로 돌려준다. UDP 에 연결이 없으므로 의미 없는 오류이고,
    로봇이 아직 안 켜졌거나 방금 재부팅한 것은 정상이다. 삼키지 않으면 측정이
    시작하자마자 죽는다 — 목업 사전 점검에서 실제로 그랬다.
    """
    sock.setblocking(False)
    try:
        while True:
            sock.recvfrom(65535)
    except (BlockingIOError, ConnectionResetError):
        pass


def run_stage(sock, send, enc, make_telegram, settle_s: float, hold_s: float):
    """자세 명령을 **한 번** 보내고, STOP 하트비트로 링크를 유지하며 관측한다.

    ⚠️ **자세 명령을 되풀이해 보내지 않는다.** 로봇은 자세를 유지하므로 하트비트는
    STOP(`move(0,0)`)으로 충분하고, 그것이 자세를 흩뜨리지도 않는다. `POSE` 를 0.2초마다
    다시 보내면 `dur` 램프가 계속 처음부터 다시 시작해 **자세가 끝내 정착하지 않는다.**
    `ACTION` 이면 더 나쁘다 — 벤더가 구간마다 delay() 로 약 1초를 블로킹하므로 되풀이
    송신은 로봇을 계속 그 안에 묶어 둔다.

    ⚠️ **`settle_s` 동안의 샘플은 버린다.** 그 구간은 움직이는 중이라 중앙값에 섞이면
    «자세의 피치» 가 아니라 «자세로 가는 도중의 평균» 이 된다.
    """
    sent_seq = None
    if make_telegram is not None:
        # ⚠️ **전문은 보내기 직전에 만든다.** 미리 만들어 두면 그 사이 하트비트가 더 큰
        # seq 로 지나가 버려, 정작 보낼 때는 규칙 ①(순서 역전·중복 폐기)에 걸려 **로봇이
        # 통째로 버린다** — 2026-09-15 실기에서 자세가 하나도 바뀌지 않은 원인이었다.
        telegram = make_telegram()
        send(telegram)
        sent_seq = int(json.loads(telegram)["seq"])

    samples, ack = [], None
    start, last_ping = time.time(), 0.0
    deadline = start + settle_s + hold_s
    while time.time() < deadline:
        if time.time() - last_ping >= KEEPALIVE_S:
            # 하트비트는 매번 새 seq 여야 한다 — 같은 전문을 되풀이하면 규칙 ① 이
            # 전부 폐기하고 로봇은 명령이 끊긴 것으로 보아 스스로 래치한다.
            send(enc.stop(), quiet=True)
            last_ping = time.time()
        try:
            data, _ = sock.recvfrom(65535)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except ConnectionResetError:
            # ⚠️ **Windows 전용이고 무시해야 하는 오류다.** 로봇이 아직 안 켜졌거나
            # 재부팅 중이면 보낸 명령에 ICMP Port Unreachable 이 돌아오고, Windows 는
            # 그것을 **다음 `recvfrom` 의** `ConnectionResetError` 로 준다. UDP 에
            # 연결이 없으므로 의미 없는 통보이며 여기서 죽으면 측정이 시작하자마자
            # 끝난다 — 목업 사전 점검에서 실제로 그랬다.
            #
            # ⚠️ **`SIO_UDP_CONNRESET` 로 끄는 방법은 CPython 에서 쓸 수 없다.**
            # `socket` 이 허용하는 ioctl 은 `SIO_KEEPALIVE_VALS`·`SIO_LOOPBACK_FAST_PATH`·
            # `SIO_RCVALL` 뿐이고, 값을 직접 넣어도 `invalid ioctl command` 로 거부된다
            # (3.12.10 확인). 저장소 여러 곳에 그 설정이 있지만 `hasattr` 가 항상 거짓이라
            # **한 번도 실행되지 않는다.** 예외를 잡는 쪽이 유일한 방어다.
            time.sleep(0.005)
            continue
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(msg, dict):
            continue
        if isinstance(msg.get("imu"), dict):
            if time.time() - start >= settle_s:  # 정착 전 구간은 버린다
                samples.append({"t": time.time(), "imu": msg["imu"], "state": msg.get("state")})
        elif msg.get("seq") == sent_seq and "applied" in msg:
            ack = msg
    return samples, ack


def summarize(samples: list[dict], label: str) -> dict:
    """부호를 살린 통계를 낸다 — 버티는 자세라 중앙값이 곧 답이고 최소·최대가 흔들림이다."""
    pitches = sorted(s["imu"]["pitch"] for s in samples if "pitch" in s["imu"])
    rolls = sorted(s["imu"]["roll"] for s in samples if "roll" in s["imu"])

    def stats(vals):
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "median": round(vals[len(vals) // 2], 2),
            "min": round(vals[0], 2),
            "max": round(vals[-1], 2),
        }

    return {"label": label, "pitch": stats(pitches), "roll": stats(rolls)}


def main() -> int:
    # ⚠️ **경고 문구 하나에 도구가 죽지 않게 한다.** 이 도구의 요점이 «로봇이
    # 실행하지 않았다» 를 알리는 것인데, 그 경고를 찍다가 cp949 콘솔에서 죽으면
    # **경고가 아니라 역추적이 남는다** (2026-09-15 실기에서 실제로 그랬다).
    survive_encoding_errors()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--cmd-port", type=int, default=5001)
    ap.add_argument("--telemetry-port", type=int, default=5101)
    ap.add_argument("--settle", type=float, default=2.0, help="자세가 정착할 때까지 버릴 초")
    ap.add_argument("--hold", type=float, default=4.0, help="정착 후 관측 초")
    ap.add_argument(
        "--retries",
        type=int,
        default=2,
        help="로봇이 실행하지 않은 단계를 다시 시도할 횟수",
    )
    ap.add_argument("--out", default=None, help="원본 JSON 경로")
    args = ap.parse_args()

    enc = CommandEncoder()
    # ⚠️ **소켓 하나로 보내고 받는다.** 펌웨어는 텔레메트리를 고정 포트(기본 5101)로
    # 보내고 ACK 는 **명령이 온 출발 포트로** 돌려준다. 보내는 소켓을 그 포트에 묶어
    # 두면 둘이 같은 자리로 들어온다 — 따로 두면 ACK 가 임시 포트로 가서 방화벽에
    # 걸리거나 아무도 읽지 않는다(2026-09-15 실기에서 ACK 를 하나도 못 받았다).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # ⚠️ **`SO_REUSEADDR` 를 쓰지 않는다 — 포트 충돌이 조용히 넘어간다.**
    # 런타임이 같은 포트를 잡고 있는데 이 옵션으로 묶으면 **bind 가 오류 없이
    # 성공하고 패킷은 하나도 안 온다.** 이 PC 에서 재현했다 — 보낸 20개 중 먼저
    # 묶은 쪽이 20개, 나중이 0개였다. 옵션을 빼면 그 자리에서 `WinError 10048` 로
    # 실패한다. 측정 도구에서 «표본 0» 은 오류로 드러나야 한다. 조용히 빈 결과가
    # 나오면 원인을 로봇 쪽에서 찾게 된다(실제로 그렇게 헤맸다).
    #
    # UDP 에는 이 옵션이 필요 없다 — TCP 의 `TIME_WAIT` 재바인드 문제가 없다.
    sock.bind(("0.0.0.0", args.telemetry_port))
    sock.setblocking(False)

    def send(msg: str, quiet: bool = False) -> None:
        sock.sendto(msg.encode(), (args.host, args.cmd_port))
        if not quiet:
            print(f"  -> {msg}", flush=True)

    # 새 세션 규약: 첫 명령은 STOP seq=1 (최신 ts 로 순서 기준 초기화)
    send(enc.stop())
    drain(sock)
    # ⚠️ **`time.sleep` 으로 기다리지 않는다.** 로봇은 300ms 침묵마다 다시 잠기므로
    # 해제 뒤 잠깐이라도 쉬면 **뒤따르는 자세 명령이 전부 무시된다** — 실제로 그렇게
    # 만들어서 로봇이 한 자세도 취하지 않았다(`gait_calibrate` 도 같은 곳에 주석이 있다).
    # 해제를 끼워 넣고 그대로 10Hz 하트비트로 넘어간다.
    run_stage(sock, send, enc, enc.reset_safe, settle_s=0.0, hold_s=1.0)

    # ⚠️ **피치 부호에 주의한다 — 음수가 «고개를 드는» 쪽이다** (PROTOCOL `POSE` 부호표).
    # 2026-09-15 IMU 실측: `+15` → 17.4(앞이 내려감), `-15` → -11.6(앞이 올라감).
    # `FR-3.3` 경계 자세와 `FR-9.2.2` 자세 상승이 쓰는 것은 «드는» 쪽이므로 음수를 훑는다.
    #
    # ⚠️ **앉기는 `POSE height` 로 흉내 내지 않는다.** `height` 는 몸통을 고르게 낮출
    # 뿐이라 앉은 자세가 아니다. 벤더 액션 `sit_dowm`(`ACTION 1`)이 진짜 앉기이며
    # `4.1.2` 로 경로가 열렸다.
    def pose(pitch):
        return lambda: enc.pose(pitch=pitch, roll=0, height=0, dur=1500)

    def action(action_id):
        return lambda: enc.encode("ACTION", id=action_id)

    # ⚠️ **기준선도 명령으로 만든다.** 아무것도 보내지 않고 재면 «직전 시험이 남긴
    # 자세» 를 기준선이라 적게 된다 — 실제로 앞 시험의 `pitch=-20` 이 그대로 남아
    # 기준선이 -17.4 로 나왔다. 먼저 세우고(`ACTION 0`) 중립 자세를 준다.
    stages = [
        ("baseline-stand", action(0)),
        ("baseline-neutral", pose(0)),
        ("nose-up-10", pose(-10)),
        ("nose-up-20", pose(-20)),
        ("nose-up-30", pose(-30)),
        ("neutral", pose(0)),
        ("sit", action(1)),
        ("restore-stand", action(0)),
    ]

    results, silent, thin = [], [], []
    for label, make_telegram in stages:
        # ⚠️ **벤더가 조용히 건너뛰면 다시 시도한다.** 펌웨어는 걸린 시간으로 실행
        # 여부를 판정해 `applied` 에 실어 줄 뿐 스스로 재시도하지 않는다 — 또 1초를
        # 블로킹할지는 호스트가 정할 일이기 때문이다(`4.1.2`). 여기가 그 자리다.
        # 특히 `POSE` 구간 바로 뒤의 `ACTION` 이 자주 건너뛰어진다(2026-09-15 실기).
        for attempt in range(args.retries + 1):
            samples, ack = run_stage(sock, send, enc, make_telegram, args.settle, args.hold)
            if ack is None or ack["applied"]:
                break
            print(f"  {label}: 로봇이 실행하지 않았다 — 재시도 {attempt + 1}/{args.retries}")
        row = summarize(samples, label)
        if ack is not None:
            row["applied"] = ack["applied"]
            # ⚠️ **벤더 액션은 실행하지 않고도 조용히 돌아온다** (`4.1.2` 실기: 43회 중
            # 6회). 그대로 두면 **서 있는 로봇의 피치를 «앉기» 로 적게 된다.** 펌웨어가
            # 걸린 시간으로 판정해 `applied` 에 실어 주므로 여기서 걸러 낸다.
            if not ack["applied"]:
                silent.append(label)
        if row["pitch"]["n"] < MIN_SAMPLES:
            thin.append(f"{label}({row['pitch']['n']})")
        results.append(row)
        print(f"{label}: pitch={row['pitch']} roll={row['roll']} n={len(samples)}", flush=True)

    send(enc.stop())
    if silent:
        print(f"\n⚠️ 로봇이 실행하지 않은 단계: {', '.join(silent)} — 그 값은 쓰지 않는다.")
    if thin:
        print()
        print(
            f"⚠️ 표본이 모자란 단계: {', '.join(thin)} (기준 {MIN_SAMPLES}개) — "
            f"텔레메트리가 억제되고 있다. UART 의 `Telemetry unavailable` 을 확인한다."
        )
    if args.out:
        payload = {"stages": results, "not_applied": silent, "too_few_samples": thin}
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"saved -> {args.out}")
    return 1 if (silent or thin) else 0


if __name__ == "__main__":
    raise SystemExit(main())
