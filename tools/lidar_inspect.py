"""LD19 실측 검사 — 실물 스캔의 통계 특성을 잰다.

`ld19_serial_relay.py` 와 같은 시리얼 경로로 읽되 UDP 송신 대신 한 바퀴
단위로 모아 통계를 낸다. 센서가 "믿을 만한가"를 정하는 데 쓴다:

- 회전 속도·프레임률 — 스펙(10Hz, 4500점/s)과 대조
- 한 바퀴당 점 수·무효점(dist=0) 비율
- 각도 빈별 거리 중앙값·표준편차 — 시간 방향 노이즈(반복 측정 산포)
- 거리 분포 — `lidar.range_min_mm`/`range_max_mm` 설정과 대조

    python tools/lidar_inspect.py --serial COM10 --scans 50
    python tools/lidar_inspect.py --serial COM10 --scans 50 --out result.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host.common.console import survive_encoding_errors
from ld19_serial_relay import Ld19Parser, ScanAssembler, CDEG_PER_REV

BIN_CDEG = 500  # 5도 빈


def run(args: argparse.Namespace) -> int:
    import serial

    parser, assembler = Ld19Parser(), ScanAssembler()
    scans: list[list[tuple[int, int]]] = []
    speeds: list[int] = []
    started = time.monotonic()

    print(f"[inspect] {args.serial} @{args.baud} — 한 바퀴 {args.scans}장 수집", flush=True)
    with serial.Serial(args.serial, args.baud, timeout=1) as port:
        while len(scans) < args.scans:
            for byte in port.read(4096):
                frame = parser.feed(byte)
                if frame is None:
                    continue
                speeds.append(frame["speed_dps"])
                done = assembler.add_frame(frame)
                if done:
                    scans.append(done)
            if time.monotonic() - started > 60:
                print("[inspect] 60초 초과 — 수집 중단", flush=True)
                break

    if not scans:
        print("[inspect] 완성된 스캔 없음", flush=True)
        return 1

    # ── 회전·프레임 통계 ──
    elapsed = time.monotonic() - started
    pts_per_scan = [len(s) for s in scans]
    invalid_ratio = [
        sum(1 for _, d in s if d == 0) / len(s) if s else 0.0 for s in scans
    ]
    # LD19 speed 필드는 deg/s 단위 그대로다 (실측 ~3580 ≈ 9.9 rev/s)

    # ── 각도 빈별 통계 (시간 방향 산포) ──
    bins: dict[int, list[int]] = {}
    for s in scans:
        for cdeg, dist in s:
            if dist > 0:
                bins.setdefault((cdeg // BIN_CDEG) * BIN_CDEG, []).append(dist)

    bin_rows = []
    for b in sorted(bins):
        ds = bins[b]
        med = statistics.median(ds)
        std = statistics.pstdev(ds) if len(ds) > 1 else 0.0
        bin_rows.append({
            "deg": b / 100.0, "n": len(ds),
            "median_mm": med, "std_mm": round(std, 1),
            "min_mm": min(ds), "max_mm": max(ds),
        })

    all_d = [d for s in scans for _, d in s if d > 0]
    noise = [r["std_mm"] for r in bin_rows if r["n"] >= 20 and r["median_mm"] < 8000]

    print(f"\n═══ 수집 — {len(scans)} 회전 / {elapsed:.1f}s ═══", flush=True)
    print(f"프레임       : {parser.frames_ok} (crc_fail={parser.crc_failures} resync={parser.resyncs})", flush=True)
    print(f"회전 속도    : {statistics.median(speeds):.0f} deg/s ≈ {statistics.median(speeds)/360:.1f} rev/s", flush=True)
    print(f"점/회전      : 중앙 {statistics.median(pts_per_scan):.0f} (min {min(pts_per_scan)} ~ max {max(pts_per_scan)})", flush=True)
    print(f"무효점 비율  : {statistics.median(invalid_ratio)*100:.1f}% (dist=0)", flush=True)
    print(f"거리 분포    : min {min(all_d)}mm · 중앙 {statistics.median(all_d):.0f}mm · max {max(all_d)}mm", flush=True)
    if noise:
        print(f"빈별 σ(측정 산포): 중앙 {statistics.median(noise):.1f}mm · p95 {sorted(noise)[int(len(noise)*0.95)]:.1f}mm", flush=True)

    print("\n═══ 각도 빈별 (5°) — n·중앙mm·σmm·범위 ═══", flush=True)
    for r in bin_rows:
        print(f"  {r['deg']:6.1f}°  n={r['n']:3d}  med={r['median_mm']:5.0f}  σ={r['std_mm']:5.1f}  [{r['min_mm']}~{r['max_mm']}]", flush=True)

    if args.out:
        report = {
            "serial": args.serial, "baud": args.baud, "scans": len(scans),
            "elapsed_s": round(elapsed, 2),
            "frames_ok": parser.frames_ok, "crc_fail": parser.crc_failures,
            "resync": parser.resyncs,
            "speed_median_raw": statistics.median(speeds),
            "pts_per_scan_median": statistics.median(pts_per_scan),
            "invalid_ratio_median": statistics.median(invalid_ratio),
            "dist_min_mm": min(all_d), "dist_max_mm": max(all_d),
            "dist_median_mm": statistics.median(all_d),
            "bin_std_median_mm": statistics.median(noise) if noise else None,
            "bins": bin_rows,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[inspect] 저장: {args.out}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LD19 실측 통계 — 시리얼로 직접 읽어 분석한다")
    p.add_argument("--serial", required=True, help="COM 포트 (예: COM10)")
    p.add_argument("--baud", type=int, default=230400)
    p.add_argument("--scans", type=int, default=50, help="수집할 회전 수")
    p.add_argument("--out", default=None, help="JSON 리포트 저장 경로")
    return p


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
