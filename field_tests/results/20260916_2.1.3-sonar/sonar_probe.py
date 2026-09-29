"""초음파 표적 거리 측정 — 런타임 /api/telemetry 에서 새 seq 의 dist_cm 만 모은다 (설정 변경 없음).

사용: python sonar_probe.py <기준거리cm 또는 이름> [초=5] [기록파일]
"""

import json
import statistics
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
label = sys.argv[1]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
out_path = sys.argv[3] if len(sys.argv) > 3 else None

seen = set()
values = []
stale = 0
batt = []
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/telemetry", timeout=2) as r:
        snap = json.load(r)
    t = snap.get("telemetry") or {}
    if snap.get("stale"):
        stale += 1
    key = (t.get("boot_id"), t.get("seq"))
    if t and key not in seen:
        seen.add(key)
        values.append(t["dist_cm"])
        batt.append(t["batt_v"])
    time.sleep(0.05)

if not values:
    line = f"{label}: 표본 0 (stale {stale})"
else:
    values_sorted = sorted(values)

    def p(q: float) -> float:
        return values_sorted[min(len(values_sorted) - 1, int(q * len(values_sorted)))]

    line = (
        f"{label}: n={len(values)} 중앙 {statistics.median(values):.1f} · 최소 {min(values):.1f} · "
        f"최대 {max(values):.1f} · p10 {p(0.1):.1f} · p90 {p(0.9):.1f} cm · "
        f"stale {stale} · 배터리 {statistics.median(batt):.2f} V"
    )
    top = Counter(round(v, 1) for v in values).most_common(4)
    line += " · 빈도 " + ", ".join(f"{v}×{c}" for v, c in top)
print(line)
if out_path:
    with Path(out_path).open("a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + line + "\n")
