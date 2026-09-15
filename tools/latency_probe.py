"""촬영→호스트 도착 구간 측정 하네스 (WBS 6.1.2 · NFR-1.1 의 첫 구간).

**호스트가 자기 화면에 자기 시계를 띄우고 카메라가 그 화면을 찍는다.** 프레임에 찍힌
값과 도착 시각의 차가 지연이며, **둘이 같은 시계라 동기가 필요 없다** (아키텍처
NFR-1.1 절).

⚠️ **이 도구가 재는 것은 NFR-1.1 전체가 아니다.** NFR-1.1 은 단일 프레임의
`촬영 → 검출 → 명령 적용 ACK` 이고, 여기서 나오는 값은 그중 **촬영 → 호스트 도착**
하나뿐이다. 디코드·추론·사람 확인(시간 창)·명령 왕복·물리 구동은 빠져 있으므로
**이 값으로 250ms 통과 판정을 내리면 안 된다** — 남은 구간이 공짜인 것처럼 보인다.

프레임에 촬영 시각을 넣는 방식은 채택하지 않았다 — XIAO 에 시계가 없어 오프셋을
추정해야 하고 그 오차가 재려는 대상(왕복)에 종속되기 때문이다.

    ① page     호스트 화면에 띄울 ms 카운터를 만든다
    ② capture  스트림에 붙어 프레임 N 장을 도착 시각과 함께 저장한다
    ③ report   프레임에 보이는 숫자를 적어 넣고 통계를 낸다

②와 ③이 나뉘어 있는 이유 — **프레임 속 숫자를 읽는 것은 사람(또는 사람 대신 보는
쪽)의 일이다.** OCR 을 넣으면 인식 실패가 측정 오차로 섞이고, 표본이 수십 장이면
눈으로 읽는 것이 더 빠르고 확실하다.
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import statistics
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402
from host.vision.stream_client import StreamReader, decode_jpeg, stream_endpoints  # noqa: E402

#: 화면에 표시하는 자릿수. epoch 밀리초 13자리는 카메라로 읽을 수 없다.
#: 5자리는 100초마다 되돌아오므로 1초 미만 지연에는 모호함이 없다.
MODULUS = 100_000

COUNTER_HTML = """<!doctype html>
<meta charset="utf-8">
<title>MechDog 지연 측정 카운터</title>
<style>
  html, body { margin: 0; background: #000; color: #9a9a9a; overflow: hidden;
               font: 700 1vw/1.4 "Consolas", "Courier New", monospace; }
  #d { font-size: 20vw; text-align: center; letter-spacing: 0.08em; margin-top: 2vh;
       color: #ffffff; }
  /* ⚠️ **연속 막대를 쓰지 않는다.** 가장자리 위치를 읽으려면 막대의 양 끝이 프레임
     안에 있어야 하고 포화되지 않아야 하는데, 실기에서 둘 다 깨졌다 — 화면을 크게
     대면 끝이 잘리고, 꽉 채우면 하얗게 떠서 가장자리가 사라졌다(2026-09-15).
     **칸으로 나누면 세기만 하면 된다** — 잘려도 남은 칸을 세고, 떠도 칸 사이 틈이
     남는다. 분해능 10ms 는 모니터 양자화(60Hz = 16.7ms)보다 어차피 촘촘하다. */
  #cells { display: flex; gap: 1.4vw; margin: 2vh 3vw; height: 20vh; }
  .cell { flex: 1; background: #0b0b0b; border: 0.5vh solid #3a3a3a; }
  .cell.on { background: #ffffff; border-color: #ffffff; }
  #h { font: 400 1.6vw/1.6 sans-serif; text-align: center; color: #6e6e6e; }
</style>
<div id="d">000</div>
<div id="cells"></div>
<div id="h">위 숫자 = 100ms 단위 · 켜진 칸 수 x 10ms 를 더한다</div>
<script>
  const d = document.getElementById("d");
  const wrap = document.getElementById("cells");
  const cells = [];
  for (let i = 0; i < 10; i++) {
    const c = document.createElement("div");
    c.className = "cell";
    wrap.appendChild(c);
    cells.push(c);
  }
  function tick() {
    // ⚠️ Date.now() 는 이 호스트의 시계다. 측정 쪽과 같은 시계이므로 동기가 필요 없다.
    //
    // ⚠️ **숫자로 하위 자리를 읽을 수 없다.** 화면은 60Hz 로만 다시 그려지고 카메라
    // 노출이 두 번의 갱신을 걸치므로 빨리 바뀌는 자리가 뭉개진다 — 실제로 4·5번째
    // 자리가 읽히지 않았다. 그래서 **느리게 바뀌는 숫자(100ms)와 칸**으로 나눈다.
    const now = Date.now();
    d.textContent = String(Math.floor(now / 100) % 1000).padStart(3, "0");
    const lit = Math.floor((now % 100) / 10);
    for (let i = 0; i < 10; i++) cells[i].classList.toggle("on", i <= lit);
    requestAnimationFrame(tick);
  }
  tick();
</script>
"""


def counter_page() -> str:
    return COUNTER_HTML


def with_xiao_ip(config: Mapping[str, Any], ip: str | None) -> dict[str, Any]:
    """`--xiao-ip` 덮어쓰기. **주소는 `network` 절 안이다.**

    ⚠️ 최상위에 넣으면 조용히 무시된다 — 실제로 그렇게 만들어서 카메라를 향하게
    두고 나서야 걸렸다. 프로파일·런타임·이 도구까지 같은 실수를 세 곳에서 했다.
    """
    merged = dict(config)
    if ip:
        merged["network"] = dict(config["network"], xiao_ip=ip)
    return merged


def latency_ms(arrival_ms: int, shown_ms: int, *, modulus: int = MODULUS) -> int:
    """도착 시각과 화면에 보인 값의 차. **되돌아옴(wrap)을 보정한다.**

    화면은 하위 5자리만 보여주므로 100초마다 0으로 돌아간다. 촬영이 되돌아옴 직전,
    도착이 직후이면 음수가 나오므로 한 주기를 더한다.
    """
    if not 0 <= shown_ms < modulus:
        raise ValueError(f"화면 값이 0~{modulus - 1} 범위를 벗어남: {shown_ms}")
    delta = (arrival_ms % modulus) - shown_ms
    if delta < 0:
        delta += modulus
    return delta


def summarize(samples: list[int], *, refresh_hz: float = 60.0) -> dict[str, Any]:
    """표본 통계. **평균만 보지 않는다** — 최악값이 예산을 결정한다."""
    if not samples:
        raise ValueError("표본이 없다")
    ordered = sorted(samples)
    index = max(0, round(0.95 * (len(ordered) - 1)))
    return {
        "n": len(ordered),
        "min_ms": ordered[0],
        "mean_ms": round(statistics.fmean(ordered), 1),
        "p95_ms": ordered[index],
        "max_ms": ordered[-1],
        # 모니터 주기만큼 양자화가 섞인다. 값을 몰래 보정하지 않고 함께 적는다.
        "quantization_ms": round(1000.0 / refresh_hz, 1),
    }


def budget_note(stats: Mapping[str, Any], budget_ms: int) -> str:
    """예산 대비 **이 구간의 몫**. 통과 판정이 아니다.

    ⚠️ 예전에는 `NFR-1.1 예산 250ms 대비 최악값 … → 통과` 를 찍었다. 이 도구는
    촬영→도착만 재므로, 부분 구간을 전체 예산에 대고 합격 도장을 찍은 것이었다.
    남은 구간(디코드·추론·연속 확인·명령 왕복·구동)을 재기 전에는 판정할 수 없다.
    """
    worst = int(stats["max_ms"])
    share = round(100.0 * worst / budget_ms, 1)
    if worst > budget_ms:
        return f"촬영→도착 최악 {worst}ms 가 이미 예산 {budget_ms}ms 를 넘었다 → **미달 확정**"
    return (
        f"촬영→도착 최악 {worst}ms = 예산 {budget_ms}ms 의 {share}%. "
        "**나머지 구간(디코드·추론·사람 확인·명령 왕복·구동)은 포함되지 않았다** "
        "— NFR-1.1 판정이 아니다"
    )


# ── ① 카운터 페이지 ─────────────────────────────────────────
def cmd_page(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    page = out / "counter.html"
    page.write_text(counter_page(), encoding="utf-8")
    print(f"카운터 페이지: {page.resolve()}")
    print("브라우저로 열고 F11 전체화면 → XIAO 를 그 화면에 향하게 둔다.")
    return 0


# ── ② 프레임 수집 ───────────────────────────────────────────
def cmd_capture(args: argparse.Namespace) -> int:
    config = with_xiao_ip(load_config(args.device), args.xiao_ip)
    endpoints = stream_endpoints(config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    reader = StreamReader(config, endpoints=endpoints)
    print(f"연결: {endpoints.stream}")
    rows: list[dict[str, Any]] = []
    # ⚠️ 연속된 프레임을 잡으면 표본이 같은 화면 갱신 주기에 몰린다. 간격을 두어
    # 모니터 주기와 우연히 동기되는 것을 피한다.
    every = max(1, args.every)
    seen = 0
    for frame in reader.frames(max_failures=args.max_failures):
        seen += 1
        if seen % every:
            continue
        name = f"frame_{len(rows) + 1:02d}.jpg"
        (out / name).write_bytes(frame.payload)
        rows.append(
            {
                "file": name,
                "arrival_ms": frame.received_ms,
                "seq": frame.seq,
                "size_bytes": frame.size_bytes,
            }
        )
        print(f"  {name}  arrival_ms={frame.received_ms}  {frame.size_bytes}B")
        if len(rows) >= args.count:
            break

    (out / "arrivals.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    readings = out / "readings.csv"
    with readings.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "arrival_ms", "shown_ms"])
        for row in rows:
            writer.writerow([row["file"], row["arrival_ms"], ""])
    print(f"\n프레임 {len(rows)}장 · 통계: {reader.stats}")
    print(f"다음: {readings.resolve()} 의 shown_ms 칸에 프레임 속 숫자를 적고 report 를 실행한다.")
    return 0 if rows else 1


# ── 전 구간 사슬 ────────────────────────────────────────────
def _await_ack(sock: socket.socket, seq: int, deadline_ms: int) -> tuple[int | None, bool]:
    """그 `seq` 의 ACK 가 올 때까지 기다린다. **텔레메트리는 흘려보낸다.**

    ⚠️ 명령을 텔레메트리 소켓에서 보내므로 응답도 그 소켓으로 돌아온다 — 같은 소켓에
    10Hz 텔레메트리가 함께 들어오므로 `seq` 로 골라야 한다. 아무 전문이나 첫 번째를
    ACK 로 세면 **텔레메트리 도착 시각을 지연이라고 적게 된다.**
    """
    while True:
        remaining = deadline_ms - system_clock_ms()
        if remaining <= 0:
            return None, False
        sock.settimeout(remaining / 1000.0)
        try:
            raw, _ = sock.recvfrom(4096)
        except (TimeoutError, OSError):
            return None, False
        now = system_clock_ms()
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not (isinstance(msg, dict) and "verdict" in msg and "applied" in msg):
            continue  # 텔레메트리다
        if int(msg.get("seq", -1)) != seq:
            continue  # 앞 명령의 ACK — 버린다
        return now, bool(msg.get("applied"))


def cmd_chain(args: argparse.Namespace) -> int:
    """한 프레임을 **도착 → 검출 → 명령 ACK** 까지 따라가며 시각을 적는다.

    ⚠️ **런타임과 동시에 돌리지 않는다** — 텔레메트리 포트를 두 프로세스가 바인드할 수
    없다. 이 도구만 단독으로 띄운다.

    ⚠️ **로봇은 움직이지 않는다.** 보내는 것은 `STOP`(`move(0,0)`) 뿐이고 서 있는
    로봇에게는 아무 일도 일어나지 않는다. 2026-09-09 기준선도 같은 방식이었다.

    ⚠️ **표본이 아닌 프레임에도 `STOP` 을 보낸다.** 표본만 보내면 간격이 온보드 300ms
    명령 타임아웃을 넘겨 **측정 도중에 로봇이 잠긴다** — 그러면 `applied` 가 거짓이
    되어 재려던 구간이 달라진다. 표본이 아닌 명령의 ACK 는 `seq` 대조에서 걸러진다.
    """
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector

    config = with_xiao_ip(load_config(args.device), args.xiao_ip)
    network = config["network"]
    robot_ip = args.robot_ip or network.get("mechdog_ip")
    if not robot_ip:
        print("로봇 IP 가 없다 - --robot-ip 를 주거나 개체 프로파일에 적는다.")
        return 1
    peer = (str(robot_ip), int(network["cmd_port"]))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    detector = Detector(config, labels=COCO_CLASSES)
    detector.open()  # 첫 프레임 122ms 문제를 여기서 털어낸다

    endpoints = stream_endpoints(config)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", int(network["telemetry_port"])))
    encoder = CommandEncoder()
    reader = StreamReader(config, endpoints=endpoints)
    print(f"연결: 카메라 {endpoints.stream} · 로봇 {peer[0]}:{peer[1]}")

    rows: list[dict[str, Any]] = []
    every = max(1, args.every)
    seen = 0
    try:
        for frame in reader.frames(max_failures=args.max_failures):
            seen += 1
            if seen % every:
                sock.sendto(encoder.stop().encode("utf-8"), peer)  # 링크 유지용
                continue
            image = decode_jpeg(frame.payload)
            detections = detector.detect(image)
            completed_ms = system_clock_ms()
            telegram = encoder.stop()
            seq = int(json.loads(telegram)["seq"])
            sock.sendto(telegram.encode("utf-8"), peer)
            sent_ms = system_clock_ms()
            ack_ms, applied = _await_ack(sock, seq, sent_ms + args.ack_timeout_ms)
            name = f"chain_{len(rows) + 1:02d}.jpg"
            (out / name).write_bytes(frame.payload)
            rows.append(
                {
                    "file": name,
                    "frame_seq": frame.seq,
                    "arrival_ms": frame.received_ms,
                    "completed_ms": completed_ms,
                    "cmd_seq": seq,
                    "sent_ms": sent_ms,
                    "ack_ms": ack_ms,
                    "applied": applied,
                    "detections": len(detections),
                    "decode_infer_ms": completed_ms - frame.received_ms,
                    "ack_ms_from_detect": None if ack_ms is None else ack_ms - completed_ms,
                }
            )
            print(
                f"  {name}  도착~검출 {rows[-1]['decode_infer_ms']}ms  "
                f"검출~ACK {rows[-1]['ack_ms_from_detect']}ms  applied={applied}"
            )
            if len(rows) >= args.count:
                break
    finally:
        sock.close()

    (out / "chain.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    readings = out / "chain_readings.csv"
    with readings.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "arrival_ms", "completed_ms", "ack_ms", "applied", "shown_ms"])
        for row in rows:
            writer.writerow(
                [
                    row["file"],
                    row["arrival_ms"],
                    row["completed_ms"],
                    row["ack_ms"],
                    row["applied"],
                    "",
                ]
            )
    missing = [r["file"] for r in rows if r["ack_ms"] is None]
    if missing:
        print(f"\n[경고] ACK 미수신 {len(missing)}건: {', '.join(missing)}")
    rejected = [r["file"] for r in rows if r["ack_ms"] is not None and not r["applied"]]
    if rejected:
        print(
            f"\n[경고] applied=false {len(rejected)}건 - 로봇이 잠겨 있으면 이렇게 나온다."
            " 안전 해제 후 다시 잰다."
        )
    print(f"\n프레임 {len(rows)}장 · 스트림 통계: {reader.stats}")
    print(f"다음: {readings.resolve()} 의 shown_ms 를 채우고 chain-report 를 실행한다.")
    return 0 if rows else 1


def chain_stats(rows: list[dict[str, Any]], *, refresh_hz: float = 60.0) -> dict[str, Any]:
    """구간별 통계와 **같은 프레임의 합**을 낸다.

    ⚠️ **구간별 p95 를 더하지 않는다.** 서로 다른 프레임의 최악값을 합치면 실제로는
    일어나지 않는 경로가 만들어진다 — 2026-09-14 기록이 그렇게 278ms 를 냈고, 그중
    한 값은 시작점이 달라 구간이 겹치기까지 했다. 합은 **프레임마다 먼저 더한 뒤**
    그 분포를 본다.
    """
    capture = [r["capture_ms"] for r in rows]
    infer = [r["decode_infer_ms"] for r in rows]
    command = [r["ack_ms_from_detect"] for r in rows]
    total = [c + i + m for c, i, m in zip(capture, infer, command, strict=True)]
    return {
        "n": len(rows),
        "촬영~도착": summarize(capture, refresh_hz=refresh_hz),
        "도착~검출": summarize(infer, refresh_hz=refresh_hz),
        "검출~ACK": summarize(command, refresh_hz=refresh_hz),
        "합계(프레임별)": summarize(total, refresh_hz=refresh_hz),
    }


def cmd_chain_report(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    with Path(args.readings).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            shown = (row.get("shown_ms") or "").strip()
            ack = (row.get("ack_ms") or "").strip()
            if not shown or not ack:
                continue
            arrival, completed = int(row["arrival_ms"]), int(row["completed_ms"])
            rows.append(
                {
                    "capture_ms": latency_ms(arrival, int(shown)),
                    "decode_infer_ms": completed - arrival,
                    "ack_ms_from_detect": int(ack) - completed,
                }
            )
    if not rows:
        print("shown_ms 와 ack_ms 가 모두 있는 행이 없다.")
        return 1
    stats = chain_stats(rows, refresh_hz=args.refresh_hz)
    print(f"NFR-1.1 단일 프레임 사슬 - 표본 {stats['n']}")
    for name in ("촬영~도착", "도착~검출", "검출~ACK", "합계(프레임별)"):
        print(f"\n[{name}]")
        for key, value in stats[name].items():
            print(f"  {key:16s} {value}")
    worst = int(stats["합계(프레임별)"]["max_ms"])
    verdict = "충족" if worst <= args.budget_ms else "미달"
    print(f"\n최악 프레임 {worst}ms / 예산 {args.budget_ms}ms -> {verdict}")
    print("[주의] 물리 구동은 NFR-1.4(명령 -> 서보 <=50ms) 로 따로 잰다 - 이 예산에 없다.")
    return 0


# ── ③ 통계 ──────────────────────────────────────────────────
def read_readings(path: Path) -> list[int]:
    """`shown_ms` 가 채워진 행만 읽어 지연으로 바꾼다."""
    samples: list[int] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            shown = (row.get("shown_ms") or "").strip()
            if not shown:
                continue
            samples.append(latency_ms(int(row["arrival_ms"]), int(shown)))
    return samples


def cmd_report(args: argparse.Namespace) -> int:
    samples = read_readings(Path(args.readings))
    if not samples:
        print("shown_ms 가 채워진 행이 없다 — 프레임 속 숫자를 먼저 적는다.")
        return 1
    stats = summarize(samples, refresh_hz=args.refresh_hz)
    print("촬영 → 호스트 도착 (NFR-1.1 의 첫 구간)")
    for key, value in stats.items():
        print(f"  {key:16s} {value}")
    print(f"\n{budget_note(stats, args.budget_ms)}")
    print("표본:", samples)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="latency_probe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    page = sub.add_parser("page", help="화면 카운터 HTML 생성")
    page.add_argument("--out", default="logs/latency")
    page.set_defaults(func=cmd_page)

    capture = sub.add_parser("capture", help="프레임을 도착 시각과 함께 저장")
    capture.add_argument("--device", required=True)
    capture.add_argument("--xiao-ip", default=None, help="개체 프로파일 값을 덮어쓴다")
    capture.add_argument("--count", type=int, default=12)
    capture.add_argument("--every", type=int, default=5, help="N 프레임마다 한 장 저장")
    capture.add_argument("--max-failures", type=int, default=3)
    capture.add_argument("--out", default="logs/latency")
    capture.set_defaults(func=cmd_capture)

    report = sub.add_parser("report", help="shown_ms 를 채운 CSV 로 통계")
    report.add_argument("--readings", default="logs/latency/readings.csv")
    report.add_argument("--refresh-hz", type=float, default=60.0)
    report.add_argument(
        "--budget-ms",
        type=int,
        default=250,
        help="NFR-1.1 전체 예산. 이 구간이 그중 몇 %를 쓰는지만 보여준다 (판정 아님)",
    )
    report.set_defaults(func=cmd_report)

    chain = sub.add_parser("chain", help="도착 → 검출 → 명령 ACK 를 한 프레임씩 따라간다")
    chain.add_argument("--device", required=True)
    chain.add_argument("--xiao-ip", default=None, help="개체 프로파일 값을 덮어쓴다")
    chain.add_argument("--robot-ip", default=None, help="개체 프로파일 값을 덮어쓴다")
    chain.add_argument("--count", type=int, default=12)
    chain.add_argument("--every", type=int, default=5, help="N 프레임마다 한 장 표본")
    chain.add_argument("--max-failures", type=int, default=3)
    chain.add_argument("--ack-timeout-ms", type=int, default=500)
    chain.add_argument("--out", default="logs/latency")
    chain.set_defaults(func=cmd_chain)

    chain_report = sub.add_parser("chain-report", help="사슬 통계 — 프레임마다 더한 뒤 분포를 본다")
    chain_report.add_argument("--readings", default="logs/latency/chain_readings.csv")
    chain_report.add_argument("--refresh-hz", type=float, default=60.0)
    chain_report.add_argument("--budget-ms", type=int, default=250, help="NFR-1.1 예산")
    chain_report.set_defaults(func=cmd_chain_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    # ⚠️ **인자 처리보다 앞이다** — cp949 콘솔에서 `--help` 조차 죽었다
    # (CONTRIBUTING 8절). 도움말은 `argparse` 가 stdout 에 쓴다.
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
