"""`ppe_live_check` 실측 세션을 **에피소드 기준**으로 다시 센다.

프레임 기준 수치(판정 가능률·실효 성공률)는 «한 사람을 몇 번 봤나» 에 따라 크게
흔들린다. 운용에서 묻는 것은 «위반한 사람을 제한시간 안에 잡았나», «정상 작업자에게
경보를 한 번이라도 울렸나» 다. 그래서 같은 원자료(`session.json`)를 에피소드로 묶어
다시 센다. 로봇·카메라·모델을 건드리지 않는 오프라인 계산이다.

    python tools/ppe/episode_eval.py TEST_MECHDOG/results/20260928_ppe-xiao/session.json
    python tools/ppe/episode_eval.py a/session.json b/session.json --timeout-s 10 --json

정의
    에피소드      같은 (구간, 방향) 이 이어지는 이벤트 묶음(연속 run). 구간이 없는(버튼을
                  누르지 않은) 프레임은 빠진다. 시작 = 그 run 의 첫 이벤트 t, 끝 = 바로 다음
                  이벤트(구간 없는 프레임 포함)의 t(마지막이면 자기 마지막 t).
    평가 기회     프레임이 확정 기준(`settings.hits_required`, 기본 3장) 이상인 에피소드.
                  그보다 적으면 구조적으로 확정이 날 수 없으므로 에피소드 지표에서 빼고
                  «제외» 로 따로 센다. 프레임 기준 집계에는 그대로 들어간다. 길이로는
                  빼지 않는다 — 창보다 짧아도 프레임이 조밀하면 런타임은 확정한다
                  (`--min-episode-s` 로 따로 켤 수 있다).
    경보(확정)    `confirmed: true` 이벤트 = 위반 확정의 **상승 에지**. 창은 구간을 바꿀
                  때만 비워지고 방향이 바뀔 때는 이어진다 — 앞 방향의 확정이 새 에피소드
                  초반까지 켜져 있으면 «이월» 로 표시하고 새 경보로 세지 않는다.
    제한시간      위반 에피소드에서 시작부터 이 시간 안(경계 포함)의 확정만 인정한다.
                  기본값은 검수 계획의 방향당 관측 시간 `orientation_step_s`(15초) — 코드에
                  PPE 판정 제한시간 설정이 따로 없고, 한 방향 에피소드가 그만큼 이어진다.
    최종 판정     위반 에피소드: 제한시간 안 확정 또는 앞 방향에서 이월돼 켜져 있는 경보
                  → 위반(이월은 판정 시간에서 뺀다), 없으면 그 안에 적합 프레임이
                  하나라도 → 적합, 아니면 확인불가(사람 미검출 포함).
                  그 밖의 에피소드: 에피소드 전체에서 같은 순서.

지표 (확인불가를 정상으로 치지 않는다)
    PPE 위반 recall        위반 에피소드 중 제한시간 안에 확정한 비율
    정상 작업자 오경보율   정상(기대=적합) 에피소드 중 경보가 한 번 이상 난 비율 — 제한시간 없음
    판정 불가율            확인불가 / 판정(기대=확인불가 칸 제외) — 프레임·에피소드 따로
    판정 가능률            1 − 판정 불가율
    조건부 정확도          판정 가능 표본 중 기대와 일치한 비율
    실효 성공률            전체 표본(기대=확인불가 포함) 중 기대와 일치한 비율
    판정 시간              위반 에피소드 시작 → 제한시간 안 첫 확정. 평균·P95(선형 보간).
                           확정 못 한(타임아웃) 건수·이월 건수를 함께 적는다.
                           정상·확인불가 에피소드는 «최종 판정» 사건이 없어 해당 없음.

프레임 기준 집계는 `ppe_live_check.verdict_tally` 를 그대로 쓴다 — 식을 두 벌 두지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    verdict_tally,
)

#: `config.vision.ppe` 의 기본값. 옛 세션에 설정이 없을 때만 쓴다.
DEFAULT_WINDOW_MS = 1500
DEFAULT_HITS_REQUIRED = 3
EPS = 1e-6


def default_timeout_s(plan: Path = DEFAULT_ACCEPTANCE_PLAN) -> float:
    """검수 계획의 방향당 관측 시간을 제한시간 기본값으로 쓴다."""
    return float(json.loads(plan.read_text(encoding="utf-8"))["orientation_step_s"])


@dataclass(slots=True)
class Episode:
    session: str
    segment: str
    orientation: str | None
    expected: str
    start: float
    duration_s: float
    frames: int
    verdicts: dict[str, int]
    alarms: int
    first_alarm_s: float | None
    carried: bool
    final: str
    latency_s: float | None
    late: bool
    excluded: str | None


def split_episodes(events: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """구간이 붙은 이벤트를 같은 (구간, 방향) 연속 run 으로 나눈다."""
    runs: list[list[dict[str, Any]]] = []
    previous = None
    for event in events:
        key = (event.get("segment"), event.get("orientation"))
        if event.get("segment") and key == previous:
            runs[-1].append(event)
        elif event.get("segment"):
            runs.append([event])
        previous = key
    return runs


def _latched(events: Sequence[dict[str, Any]], hits_required: int) -> list[bool]:
    """이벤트마다 «그 프레임 뒤 경보가 켜져 있나» 를 `hits` 로 되살린다.

    `ViolationWindow.observe` 와 같은 규칙이다 — 창 안 히트가 기준 이상이면 켜지고,
    창이 완전히 비어야 꺼진다. 구간이 바뀌면 도구가 창을 reset 하므로 여기서도 끈다.
    ⚠️ 같은 구간 버튼을 다시 눌러 생긴 reset 은 이벤트에 남지 않아 볼 수 없다.
    """
    state, segment, out = False, None, []
    for event in events:
        if event.get("segment") != segment:
            state, segment = False, event.get("segment")
        hits = int(event.get("hits", 0))
        if hits >= hits_required:
            state = True
        elif hits == 0:
            state = False
        out.append(state)
    return out


def _final(frames: Iterable[dict[str, Any]], alarmed: bool) -> str:
    if alarmed:
        return STATE_VIOLATION
    if any(STATE_OK in f.get("states", []) for f in frames):
        return STATE_OK
    return STATE_UNKNOWN


def _excluded(
    frames: int, hits_required: int, duration: float, min_episode_s: float | None
) -> str | None:
    if frames < hits_required:
        return f"프레임 {frames}장 < 확정 기준 {hits_required}장"
    if min_episode_s is not None and duration < min_episode_s - EPS:
        return f"최소 길이 {min_episode_s:g}초 미만"
    return None


def evaluate_session(
    label: str,
    session: dict[str, Any],
    *,
    timeout_s: float,
    min_episode_s: float | None = None,
) -> list[Episode]:
    settings = session.get("settings", {})
    hits_required = int(settings.get("hits_required", DEFAULT_HITS_REQUIRED))
    events = session.get("events", [])
    latched = _latched(events, hits_required)
    position = {id(e): i for i, e in enumerate(events)}

    episodes: list[Episode] = []
    runs = split_episodes(events)
    for run in runs:
        first = position[id(run[0])]
        last = position[id(run[-1])]
        start = float(run[0]["t"])
        end = float(events[last + 1]["t"]) if last + 1 < len(events) else float(run[-1]["t"])
        before = events[first - 1] if first else None
        carried = bool(
            before is not None and before.get("segment") == run[0]["segment"] and latched[first - 1]
        )
        verdicts = {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0}
        for event in run:
            for state in event.get("states", []):
                verdicts[state] = verdicts.get(state, 0) + 1
        alarm_times = [round(float(e["t"]) - start, 3) for e in run if e.get("confirmed")]
        expected = run[0].get("expected") or STATE_UNKNOWN

        latency = None
        late = False
        if expected == STATE_VIOLATION:
            in_time = [t for t in alarm_times if t <= timeout_s + EPS]
            window = [e for e in run if float(e["t"]) - start <= timeout_s + EPS]
            # 앞 방향의 확정이 켜진 채 넘어왔으면 경보가 울리는 중이다 — 놓친 것이 아니다.
            final = _final(window, bool(in_time) or carried)
            latency = in_time[0] if in_time and not carried else None
            late = not in_time and bool(alarm_times)
        else:
            final = _final(run, bool(alarm_times))

        duration = round(end - start, 3)
        episodes.append(
            Episode(
                session=label,
                segment=run[0]["segment"],
                orientation=run[0].get("orientation"),
                expected=expected,
                start=start,
                duration_s=duration,
                frames=len(run),
                verdicts=verdicts,
                alarms=len(alarm_times),
                first_alarm_s=alarm_times[0] if alarm_times else None,
                carried=carried,
                final=final,
                latency_s=latency,
                late=late,
                excluded=_excluded(len(run), hits_required, duration, min_episode_s),
            )
        )
    return episodes


def percentile(values: Sequence[float], q: float) -> float | None:
    """선형 보간 백분위 (numpy 기본 `linear` 와 같다)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _rates(tally: dict[str, int]) -> dict[str, float | None]:
    covered = tally["coverage_count"]
    return {
        "unknown_rate": _ratio(covered - tally["coverage_determinate"], covered),
        "coverage": _ratio(tally["coverage_determinate"], covered),
        "conditional": _ratio(tally["coverage_right"], tally["coverage_determinate"]),
        "effective": _ratio(tally["right"], tally["count"]),
    }


def _sum_tallies(tallies: Iterable[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for tally in tallies:
        for key, value in tally.items():
            total[key] = total.get(key, 0) + value
    return total or verdict_tally(STATE_OK, {})


def metrics(episodes: Sequence[Episode]) -> dict[str, Any]:
    """에피소드 묶음 → 프레임 기준·에피소드 기준 지표."""
    frame = _sum_tallies(verdict_tally(e.expected, e.verdicts) for e in episodes)
    counted = [e for e in episodes if e.excluded is None]
    episode = _sum_tallies(verdict_tally(e.expected, {e.final: 1}) for e in counted)
    violations = [e for e in counted if e.expected == STATE_VIOLATION]
    normals = [e for e in counted if e.expected == STATE_OK]
    detected = [e for e in violations if e.final == STATE_VIOLATION]
    false_alarms = [e for e in normals if e.alarms]
    latencies = [e.latency_s for e in detected if e.latency_s is not None]
    return {
        "frame": {**frame, **_rates(frame)},
        "episode": {
            **episode,
            **_rates(episode),
            "episodes": len(counted),
            "excluded": len(episodes) - len(counted),
            "violation_episodes": len(violations),
            "normal_episodes": len(normals),
            "unknown_expected": sum(e.expected == STATE_UNKNOWN for e in counted),
            "detected": len(detected),
            "recall": _ratio(len(detected), len(violations)),
            "late": sum(e.late for e in violations),
            "false_alarms": len(false_alarms),
            "false_alarm_rate": _ratio(len(false_alarms), len(normals)),
            "carried": sum(e.carried for e in counted),
            "latency": {
                "n": len(latencies),
                "mean_s": sum(latencies) / len(latencies) if latencies else None,
                "p95_s": percentile(latencies, 0.95),
                "untimed": len(violations) - len(detected),
                "carried": sum(e.carried for e in detected),
            },
        },
    }


def evaluate(
    sessions: Sequence[tuple[str, dict[str, Any]]],
    *,
    timeout_s: float,
    min_episode_s: float | None = None,
) -> dict[str, Any]:
    """세션 여러 개를 합쳐 (구간, 방향) 별 · 구간별 · 전체 지표를 낸다."""
    episodes: list[Episode] = []
    described = []
    for label, session in sessions:
        found = evaluate_session(label, session, timeout_s=timeout_s, min_episode_s=min_episode_s)
        episodes += found
        settings = session.get("settings", {})
        described.append(
            {
                "path": label,
                "device": session.get("device"),
                "scenario": session.get("scenario"),
                "model_sha256": settings.get("model_sha256"),
                "window_ms": settings.get("window_ms"),
                "hits_required": settings.get("hits_required"),
                "episodes": len(found),
            }
        )

    warnings = []
    shas = {s["model_sha256"] for s in described}
    if len(shas) > 1:
        warnings.append(
            "세션마다 모델이 다르다 — 섞인 수치다. 모델별로 따로 돌린다: "
            + ", ".join(f"{s['path']}={str(s['model_sha256'])[:12]}" for s in described)
        )
    rules = {(s["window_ms"], s["hits_required"]) for s in described}
    if len(rules) > 1:
        warnings.append(
            f"세션마다 위반 확정 조건(window_ms, hits)이 다르다: {sorted(rules, key=str)}"
        )

    rows: dict[tuple[str, str | None], list[Episode]] = {}
    segments: dict[str, list[Episode]] = {}
    for e in episodes:
        rows.setdefault((e.segment, e.orientation), []).append(e)
        segments.setdefault(e.segment, []).append(e)

    return {
        "definitions": __doc__.split("정의", 1)[1].strip("\n").rstrip() if __doc__ else "",
        "settings": {"timeout_s": float(timeout_s), "min_episode_s": min_episode_s},
        "sessions": described,
        "warnings": warnings,
        "rows": [
            {"segment": s, "orientation": o, "expected": group[0].expected, **metrics(group)}
            for (s, o), group in rows.items()
        ],
        "segments": [
            {"segment": s, "expected": group[0].expected, **metrics(group)}
            for s, group in segments.items()
        ],
        "total": metrics(episodes),
        "episodes": [asdict(e) for e in episodes],
    }


# ── 출력 ──────────────────────────────────────────────────────


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _sec(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}초"


def _rate_cell(numerator: int, denominator: int, value: float | None) -> str:
    return "-" if value is None else f"{_pct(value)} ({numerator}/{denominator})"


def _episode_cells(ep: dict[str, Any]) -> list[str]:
    latency = ep["latency"]
    if ep["violation_episodes"]:
        timing = (
            f"{_sec(latency['mean_s'])} / {_sec(latency['p95_s'])} "
            f"(n={latency['n']} · 미확정 {latency['untimed']} · 늦은 확정 {ep['late']}"
            f" · 이월 {ep['carried']})"
        )
    else:
        timing = "해당 없음"
    return [
        f"{ep['episodes']}" + (f" (+제외 {ep['excluded']})" if ep["excluded"] else ""),
        _rate_cell(ep["detected"], ep["violation_episodes"], ep["recall"]),
        _rate_cell(ep["false_alarms"], ep["normal_episodes"], ep["false_alarm_rate"]),
        _pct(ep["unknown_rate"]),
        _pct(ep["coverage"]),
        _pct(ep["conditional"]),
        _pct(ep["effective"]),
        timing,
    ]


def _frame_cells(frame: dict[str, Any]) -> list[str]:
    return [
        f"{frame['count']}",
        _pct(frame["unknown_rate"]),
        _pct(frame["coverage"]),
        _pct(frame["conditional"]),
        _pct(frame["effective"]),
    ]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    settings = summary["settings"]
    lines = ["# PPE 에피소드 기준 평가", ""]
    for s in summary["sessions"]:
        lines.append(
            f"- 세션 `{s['path']}` · {s['device']} · {s['scenario']} · 모델 "
            f"`{str(s['model_sha256'])[:12]}` · 확정 {s['window_ms']}ms 안 {s['hits_required']}회"
            f" · 에피소드 {s['episodes']}개"
        )
    min_s = settings["min_episode_s"]
    lines += [
        "",
        f"제한시간 {settings['timeout_s']:g}초 (위반 에피소드 시작부터, 경계 포함) · 평가 기회 = "
        + "프레임이 확정 기준 이상인 에피소드"
        + ("" if min_s is None else f" 중 {min_s:g}초 이상 이어진 것"),
        "",
    ]
    for warning in summary["warnings"]:
        lines += [f"⚠️ {warning}", ""]

    total = summary["total"]
    ep, frame = total["episode"], total["frame"]
    lines += [
        "## 전체",
        "",
        f"- PPE 위반 recall {_rate_cell(ep['detected'], ep['violation_episodes'], ep['recall'])}"
        f" · 늦은 확정 {ep['late']}",
        "- 정상 작업자 오경보율 "
        + _rate_cell(ep["false_alarms"], ep["normal_episodes"], ep["false_alarm_rate"]),
        f"- 판정 불가율 — 프레임 {_pct(frame['unknown_rate'])} · 에피소드 {_pct(ep['unknown_rate'])}",
        f"- 판정 가능률 — 프레임 {_pct(frame['coverage'])} · 에피소드 {_pct(ep['coverage'])}",
        f"- 조건부 정확도 — 프레임 {_pct(frame['conditional'])} · 에피소드 {_pct(ep['conditional'])}",
        f"- 실효 성공률 — 프레임 {_pct(frame['effective'])} · 에피소드 {_pct(ep['effective'])}",
        "- 판정 시간(평균 / P95) — " + _episode_cells(ep)[-1],
        f"- 에피소드 {ep['episodes']}개 (위반 {ep['violation_episodes']} · 정상 "
        f"{ep['normal_episodes']} · 기대 확인불가 {ep['unknown_expected']}) · 제외 {ep['excluded']}"
        f" · 이월 {ep['carried']}",
        "",
        "## 에피소드 기준",
        "",
    ]
    header = [
        "구간",
        "방향",
        "기대",
        "에피소드",
        "recall",
        "오경보율",
        "판정 불가율",
        "판정 가능률",
        "조건부 정확도",
        "실효 성공률",
        "판정 시간 평균 / P95",
    ]
    body = [
        [r["segment"], r["orientation"] or "-", r["expected"]] + _episode_cells(r["episode"])
        for r in summary["rows"]
    ]
    body += [
        [f"**{s['segment']}**", "전체", s["expected"]] + _episode_cells(s["episode"])
        for s in summary["segments"]
    ]
    body.append(["**전체**", "", ""] + _episode_cells(ep))
    lines += _table(header, body)

    lines += ["", "## 프레임 기준 (ppe_live_check 구간 집계와 같은 식)", ""]
    header = [
        "구간",
        "방향",
        "기대",
        "판정",
        "판정 불가율",
        "판정 가능률",
        "조건부 정확도",
        "실효 성공률",
    ]
    body = [
        [r["segment"], r["orientation"] or "-", r["expected"]] + _frame_cells(r["frame"])
        for r in summary["rows"]
    ]
    body += [
        [f"**{s['segment']}**", "전체", s["expected"]] + _frame_cells(s["frame"])
        for s in summary["segments"]
    ]
    body.append(["**전체**", "", ""] + _frame_cells(frame))
    lines += _table(header, body)

    lines += ["", "## 에피소드 목록", ""]
    header = [
        "세션",
        "구간",
        "방향",
        "시작",
        "길이",
        "프레임",
        "경보",
        "첫 경보",
        "이월",
        "최종",
        "비고",
    ]
    body = []
    for e in summary["episodes"]:
        note = e["excluded"] or ("제한시간 뒤 확정" if e["late"] else "")
        body.append(
            [
                Path(e["session"]).parent.name or e["session"],
                e["segment"],
                e["orientation"] or "-",
                _sec(e["start"]),
                _sec(e["duration_s"]),
                str(e["frames"]),
                str(e["alarms"]),
                _sec(e["first_alarm_s"]),
                "예" if e["carried"] else "",
                e["final"],
                note,
            ]
        )
    lines += _table(header, body)
    lines += ["", "## 정의", "", "```text", summary["definitions"], "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PPE 실측 세션의 에피소드 기준 평가")
    parser.add_argument("sessions", nargs="+", type=Path, help="ppe_live_check 의 session.json")
    parser.add_argument(
        "--timeout-s",
        type=float,
        help="위반 확정 제한시간(초). 기본 = config/ppe_acceptance.json 의 orientation_step_s",
    )
    parser.add_argument(
        "--min-episode-s",
        type=float,
        help="이보다 짧은 에피소드도 평가 기회에서 뺀다. 기본 = 길이로는 빼지 않는다",
    )
    parser.add_argument("--json", action="store_true", help="Markdown 대신 JSON 으로 낸다")
    parser.add_argument("--out", type=Path, help="결과를 쓸 파일. 없으면 표준출력")
    args = parser.parse_args(argv)

    timeout_s = args.timeout_s if args.timeout_s is not None else default_timeout_s()
    loaded = [(str(p), json.loads(p.read_text(encoding="utf-8"))) for p in args.sessions]
    summary = evaluate(loaded, timeout_s=timeout_s, min_episode_s=args.min_episode_s)
    text = (
        json.dumps(summary, ensure_ascii=False, indent=2) if args.json else render_markdown(summary)
    )
    # 한국어 Windows 콘솔(cp949)에서 «—» 로 죽지 않게 한다 (ppe_live_check 와 같은 가드).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    for warning in summary["warnings"]:
        print(f"⚠️ {warning}", file=sys.stderr)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
