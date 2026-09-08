"""E2E 지연 측정 하네스 (WBS 6.1.2 · NFR-1.1).

**호스트가 자기 화면에 자기 시계를 띄우고 카메라가 그 화면을 찍는다.** 프레임에 찍힌
값과 도착 시각의 차가 지연이며, **둘이 같은 시계라 동기가 필요 없다** (아키텍처
NFR-1.1 절).

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
import statistics
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_config  # noqa: E402
from host.vision.stream_client import StreamReader, stream_endpoints  # noqa: E402

#: 화면에 표시하는 자릿수. epoch 밀리초 13자리는 카메라로 읽을 수 없다.
#: 5자리는 100초마다 되돌아오므로 1초 미만 지연에는 모호함이 없다.
MODULUS = 100_000

COUNTER_HTML = """<!doctype html>
<meta charset="utf-8">
<title>MechDog 지연 측정 카운터</title>
<style>
  html, body { margin: 0; background: #000; color: #9a9a9a; overflow: hidden;
               font: 700 1vw/1.4 "Consolas", "Courier New", monospace; }
  #d { font-size: 16vw; text-align: center; letter-spacing: 0.08em; margin-top: 4vh; }
  #track { position: relative; height: 22vh; margin: 3vh 2vw; background: #101010;
           border: 0.4vh solid #4a4a4a; }
  #bar { position: absolute; left: 0; top: 0; bottom: 0; width: 0; background: #8e8e8e; }
  .tick { position: absolute; top: 0; bottom: 0; width: 0.5vw; background: #d8d8d8; }
  #h { font: 400 1.5vw/1.6 sans-serif; text-align: center; color: #6e6e6e; }
</style>
<div id="d">000</div>
<div id="track"><div id="bar"></div></div>
<div id="h">위 숫자 = 100ms 단위 · 막대 = 그 안의 0~100ms (눈금 10ms)</div>
<script>
  const d = document.getElementById("d");
  const bar = document.getElementById("bar");
  const track = document.getElementById("track");
  for (let i = 1; i < 10; i++) {
    const t = document.createElement("div");
    t.className = "tick";
    t.style.left = (i * 10) + "%";
    track.appendChild(t);
  }
  function tick() {
    // ⚠️ Date.now() 는 이 호스트의 시계다. 측정 쪽과 같은 시계이므로 동기가 필요 없다.
    //
    // ⚠️ **숫자로 하위 자리를 읽을 수 없다.** 화면은 60Hz 로만 다시 그려지고 카메라
    // 노출이 두 번의 갱신을 걸치므로 빨리 바뀌는 자리가 뭉개진다 — 실제로 4·5번째
    // 자리가 읽히지 않았다. 그래서 **느리게 바뀌는 숫자(100ms)와 연속적인 막대**로
    // 나눈다. 막대는 흐려져도 가장자리 위치가 남으므로 눈금으로 읽을 수 있다.
    const now = Date.now();
    d.textContent = String(Math.floor(now / 100) % 1000).padStart(3, "0");
    bar.style.width = (now % 100) + "%";
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
    print("E2E 지연 (촬영 → 호스트 도착)")
    for key, value in stats.items():
        print(f"  {key:16s} {value}")
    budget = args.budget_ms
    verdict = "통과" if stats["max_ms"] <= budget else "미달"
    print(f"\nNFR-1.1 예산 {budget}ms 대비 최악값 {stats['max_ms']}ms → **{verdict}**")
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
    report.add_argument("--budget-ms", type=int, default=250)
    report.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
