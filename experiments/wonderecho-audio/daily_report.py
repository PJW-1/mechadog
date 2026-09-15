"""일일 음성·순찰 리포트 생성 (WBS 4.7.12).

`eventlog` 가 남긴 날짜별 JSONL 을 읽어 하루 요약을 만든다 — 대화 수,
실행된 시나리오, 경고·비상, 음성 명령 결과를 모아 마크다운으로 출력한다.

    python daily_report.py                      # 오늘 요약을 콘솔에
    python daily_report.py --date 2026-09-15 --out reports/

저널 파일이 없는 날은 "기록 없음" 요약을 돌려준다 — 실패가 아니다.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
import time
from collections import Counter
from pathlib import Path

from eventlog import load_events

DEFAULT_DIR = Path(__file__).with_name("logs")

_CMD = re.compile(r"^명령 (\S+): (.*)")
_SCN_RUN = re.compile(r"^시나리오 실행: (\S+)")
_SCN_FAIL = re.compile(r"^시나리오 실패: (\S+): (.*)")
_EMERGENCY = re.compile(r"^비상: (.*)")
#: 경고성 시스템 이벤트를 가르는 표지 — 경고·미착용·미등록·거부 계열 문구.
_WARN = re.compile(r"경고|미착용|미등록|거부|미확인|위반")


def summarize(events: list[dict], date: str = "") -> dict:
    """이벤트 목록을 집계한다. role 이 없는 줄은 load_events 가 이미 걸렀다."""
    by_role = Counter(e["role"] for e in events)
    scenarios = Counter()
    failures = []
    commands = Counter()
    emergencies = []
    warnings = []
    for e in events:
        if e["role"] != "system":
            continue
        text = e.get("text", "")
        if m := _SCN_RUN.match(text):
            scenarios[m.group(1)] += 1
        elif m := _SCN_FAIL.match(text):
            failures.append({"ts": e.get("ts", ""), "scenario": m.group(1), "error": m.group(2)})
        elif m := _CMD.match(text):
            commands[m.group(1)] += 1
        elif m := _EMERGENCY.match(text):
            emergencies.append({"ts": e.get("ts", ""), "text": m.group(1)})
        elif _WARN.search(text):
            warnings.append({"ts": e.get("ts", ""), "text": text})
    return {
        "date": date,
        "total": len(events),
        "by_role": dict(by_role),
        "conversations": by_role.get("user", 0),
        "scenario_runs": dict(scenarios),
        "scenario_failures": failures,
        "commands": dict(commands),
        "emergencies": emergencies,
        "warnings": warnings,
        "first_ts": events[0].get("ts", "") if events else "",
        "last_ts": events[-1].get("ts", "") if events else "",
    }


def render_markdown(summary: dict) -> str:
    """요약을 사람이 읽는 마크다운으로."""
    date = summary["date"] or "(날짜 미지정)"
    lines = [f"# 음성·순찰 일일 리포트 — {date}", ""]
    if not summary["total"]:
        lines += ["기록된 이벤트가 없습니다."]
        return "\n".join(lines)
    lines += [
        f"- 기록 구간: {summary['first_ts']} ~ {summary['last_ts']}",
        f"- 전체 이벤트: {summary['total']}건",
        f"- 발화(사용자): {summary['by_role'].get('user', 0)}건 / 응답(로봇): {summary['by_role'].get('robot', 0)}건 / 관제 공지: {summary['by_role'].get('admin', 0)}건",
        "",
    ]
    if summary["scenario_runs"]:
        lines.append("## 시나리오 실행")
        lines += [f"- {name}: {n}회" for name, n in summary["scenario_runs"].items()]
        lines.append("")
    if summary["commands"]:
        lines.append("## 음성 명령")
        lines += [f"- {name}: {n}건" for name, n in summary["commands"].items()]
        lines.append("")
    if summary["emergencies"]:
        lines.append("## 비상 접수")
        lines += [f"- {e['ts']} {e['text']}" for e in summary["emergencies"]]
        lines.append("")
    if summary["warnings"]:
        lines.append(f"## 경고·보안 이벤트 ({len(summary['warnings'])}건)")
        lines += [f"- {w['ts']} {w['text']}" for w in summary["warnings"]]
        lines.append("")
    if summary["scenario_failures"]:
        lines.append("## 시나리오 실패")
        lines += [
            f"- {f['ts']} {f['scenario']}: {f['error']}" for f in summary["scenario_failures"]
        ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"), help="YYYY-MM-DD")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="저널 디렉터리")
    ap.add_argument("--out", default="", help="마크다운을 쓸 디렉터리 (없으면 콘솔만)")
    args = ap.parse_args(argv)
    path = Path(args.dir) / f"voice-{args.date}.jsonl"
    summary = summarize(load_events(path), date=args.date)
    report = render_markdown(summary)
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(errors="replace")  # cp949 콘솔에서 — 등이 깨지지 않게
    print(report, end="")
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"voice-report-{args.date}.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"[report] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
