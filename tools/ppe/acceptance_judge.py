"""`ppe_live_check` 실측 세션에 PPE 합격 기준 C1~C4 를 대 보고 PASS/FAIL 을 낸다.

기준은 **측정 전에 고정**했다(2026-10-05 · WBS 3.7.3). 임계값은 코드가 아니라
`config/ppe_acceptance.json` 의 시나리오 `criteria` 에 있다. 로봇·카메라·모델을 건드리지
않는 오프라인 계산이다.

    python tools/ppe/acceptance_judge.py field_tests/results/<세션 폴더>/session.json

    C1  적합 기대 구간(직립·웅크림 전부 착용, pitch-up, sit)의 위반 확정 합계가 상한 이하
    C2  위반 기대 구간마다 위반이 확정된 방향 수가 하한 이상
    C3  전신 구간(머리 잘림 구간 제외) 전체의 판정 가능률(확인불가가 아닌 판정 / 전체 판정)이 하한 이상
    C4  머리 잘림 구간의 위반 확정이 상한 이하

«위반 확정» 은 운용 창(`settings.window_ms` / `hits_required`)이 확정한 상승 에지
(`confirmed: true`)다. C2 의 방향 판정은 `episode_eval` 의 에피소드 최종 판정을 그대로
쓴다 — 앞 방향의 확정이 켜진 채 넘어온 방향도 경보가 울리는 중이므로 확정으로 본다.
에피소드가 `episode_eval` 의 제외(확정 기준보다 프레임이 적음)이면 C2 의 방향 수와 커버리지에
세지 않는다. C1·C4 의 경보에는 그대로 센다(보수적인 쪽).

모든 구간을 모든 방향으로 찍지 않은 세션(커버리지 누락)과 운용 창이 아닌 세션은 합격 판정에
쓸 수 없다. 종료 코드는 모두 통과하면 0, 기준 실패·커버리지 누락·운용 창 문제가 있으면 1,
입력 파일·계획을 읽을 수 없으면 2 이다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ppe.episode_eval import evaluate_session  # noqa: E402
from tools.ppe.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    load_acceptance_plan,
)

CRITERIA_KEYS = (
    "window_ms",
    "hits_required",
    "max_ok_segment_alarms",
    "min_violation_directions",
    "min_decidable_rate",
    "max_clipped_alarms",
    "clipped_segment",
)


@dataclass(frozen=True, slots=True)
class Criterion:
    key: str
    name: str
    passed: bool
    detail: str


def load_criteria(path: Path, scenario: str) -> dict[str, Any]:
    """시나리오의 합격 기준을 검수 계획 정본에서 읽는다. 빠진 값은 거부한다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    criteria = data.get("scenarios", {}).get(scenario, {}).get("criteria")
    if not criteria:
        raise ValueError(f"합격 기준 없음: 시나리오 {scenario} ({path})")
    missing = [key for key in CRITERIA_KEYS if key not in criteria]
    if missing:
        raise ValueError(f"합격 기준에 빠진 값 {missing}: 시나리오 {scenario} ({path})")
    for key in CRITERIA_KEYS:
        if key == "clipped_segment":
            continue
        value = criteria[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ValueError(f"합격 기준 {key} 는 0 이상의 숫자여야 함: {value!r} ({path})")
    spec = {s.get("key"): s for s in data["scenarios"][scenario].get("segments", [])}.get(
        criteria["clipped_segment"]
    )
    if spec is None or spec.get("expected") != STATE_UNKNOWN:
        raise ValueError(
            f"clipped_segment 는 기대가 {STATE_UNKNOWN} 인 구간이어야 함: "
            f"{criteria['clipped_segment']!r} ({path})"
        )
    return criteria


def operating_window_problems(session: dict[str, Any], criteria: dict[str, Any]) -> list[str]:
    """세션이 고정 기준의 운용 창으로 잰 것인지 본다. 덮어쓴 창은 C1·C2 를 느슨하게 만든다."""
    settings = session.get("settings", {})
    problems = []
    if settings.get("overridden"):
        problems.append("--window-ms/--hits 로 덮어쓴 세션이다")
    for key in ("window_ms", "hits_required"):
        if settings.get(key) != criteria[key]:
            problems.append(f"{key} {settings.get(key)} != 기준 {criteria[key]}")
    return problems


def coverage_gaps(
    session: dict[str, Any],
    specs: list[dict[str, str]],
    orientations: list[str],
    orientation_step_s: float,
) -> list[tuple[str, str]]:
    """제외되지 않은 에피소드가 없는 (구간, 방향) 쌍을 계획 순서대로 돌려준다."""
    episodes = evaluate_session("session", session, timeout_s=orientation_step_s)
    covered = {(ep.segment, ep.orientation) for ep in episodes if ep.excluded is None}
    return [
        (spec["key"], orientation)
        for spec in specs
        for orientation in orientations
        if (spec["key"], orientation) not in covered
    ]


def judge_session(
    session: dict[str, Any],
    specs: list[dict[str, str]],
    criteria: dict[str, Any],
    orientation_step_s: float,
) -> list[Criterion]:
    expected = {spec["key"]: spec["expected"] for spec in specs}
    clipped = criteria["clipped_segment"]
    episodes = evaluate_session("session", session, timeout_s=orientation_step_s)
    alarms: dict[str, int] = {}
    directions: dict[str, set[str | None]] = {}
    carried: dict[str, set[str | None]] = {}
    for ep in episodes:
        alarms[ep.segment] = alarms.get(ep.segment, 0) + ep.alarms
        if ep.final == STATE_VIOLATION and ep.excluded is None:
            directions.setdefault(ep.segment, set()).add(ep.orientation)
            if ep.carried:
                carried.setdefault(ep.segment, set()).add(ep.orientation)

    ok_keys = [k for k, e in expected.items() if e == STATE_OK]
    bad_keys = [k for k, e in expected.items() if e == STATE_VIOLATION]
    body_keys = [k for k in expected if k != clipped]

    # ⚠️ 관측 없는 구간은 «경보 0회» 로 통과시키지 않는다 — 11구간을 다 찍어야 한다.
    seen = {
        k for k, row in session.get("segments", {}).items() if sum(row.get("verdicts", {}).values())
    }
    unseen_ok = [k for k in ok_keys if k not in seen]
    ok_alarms = sum(alarms.get(k, 0) for k in ok_keys)
    limit = criteria["max_ok_segment_alarms"]
    c1 = Criterion(
        "C1",
        "적합 구간 위반 확정",
        ok_alarms <= limit and not unseen_ok,
        f"{ok_alarms}회 (상한 {limit}) — "
        + ", ".join(f"{k} {alarms.get(k, 0)}" for k in ok_keys)
        + (f" — 관측 없음: {', '.join(unseen_ok)}" if unseen_ok else ""),
    )

    need = criteria["min_violation_directions"]
    counts = {k: len(directions.get(k, set())) for k in bad_keys}
    short = [k for k, n in counts.items() if n < need]
    c2 = Criterion(
        "C2",
        "위반 구간 방향별 확정",
        not short,
        ", ".join(
            f"{k} {n}방향" + (f"(이월 {len(carried[k])})" if k in carried else "")
            for k, n in counts.items()
        )
        + (f" — 하한 {need}방향 미달: {', '.join(short)}" if short else f" (하한 {need}방향)"),
    )

    determinate = total = 0
    for key in body_keys:
        verdicts = session.get("segments", {}).get(key, {}).get("verdicts", {})
        count = sum(verdicts.values())
        total += count
        determinate += count - verdicts.get(STATE_UNKNOWN, 0)
    floor = criteria["min_decidable_rate"]
    rate = determinate / total if total else None
    c3 = Criterion(
        "C3",
        "전신 구간 판정 가능률",
        rate is not None and rate >= floor - 1e-9,
        f"{determinate}/{total} = {rate:.1%} (하한 {floor:.0%})"
        if rate is not None
        else f"판정 없음 (하한 {floor:.0%})",
    )

    clipped_alarms = alarms.get(clipped, 0)
    cap = criteria["max_clipped_alarms"]
    c4 = Criterion(
        "C4",
        f"{clipped} 위반 확정",
        clipped_alarms <= cap and clipped in seen,
        f"{clipped_alarms}회 (상한 {cap})" + ("" if clipped in seen else " — 관측 없음"),
    )
    return [c1, c2, c3, c4]


def render(
    session: dict[str, Any],
    results: list[Criterion],
    source: str,
    gaps: list[tuple[str, str]] | None = None,
    problems: list[str] | None = None,
) -> str:
    settings = session.get("settings", {})
    lines = [
        f"PPE 합격 기준 판정 — {source}",
        f"  기체 {session.get('device')} · 모델 sha256 {settings.get('model_sha256')}",
        f"  위반 확정 {settings.get('window_ms')}ms 안 {settings.get('hits_required')}회",
        "",
    ]
    for c in results:
        lines.append(f"{c.key} {'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}")
    if gaps:
        lines.append(
            f"커버리지 누락 {len(gaps)}칸 (구간·방향): "
            + ", ".join(f"{segment}/{orientation}" for segment, orientation in gaps)
        )
    for problem in problems or []:
        lines.append(f"⚠️ 운용 창이 아니다 — 합격 판정에 쓰지 않는다: {problem}")
    lines.append("")
    reasons = [f"{c.key} 실패" for c in results if not c.passed]
    if gaps:
        reasons.append(f"커버리지 누락 {len(gaps)}칸")
    if problems:
        reasons.append("운용 창 아님")
    lines.append("전체 PASS" if not reasons else "전체 FAIL — " + ", ".join(reasons))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PPE 합격 기준 C1~C4 자동 판정")
    parser.add_argument("session", type=Path, help="ppe_live_check 가 남긴 session.json")
    parser.add_argument("--plan", type=Path, default=DEFAULT_ACCEPTANCE_PLAN)
    parser.add_argument("--scenario", help="기본 = 세션에 적힌 시나리오")
    args = parser.parse_args(argv)

    try:
        session = json.loads(args.session.read_text(encoding="utf-8"))
        scenario = args.scenario or session.get("scenario") or "xiao"
        specs, step_s, orientations = load_acceptance_plan(args.plan, scenario)
        criteria = load_criteria(args.plan, scenario)
    except (OSError, ValueError) as exc:  # JSONDecodeError 는 ValueError 의 하위 클래스
        print(f"입력 오류: {exc}", file=sys.stderr)
        return 2
    results = judge_session(session, specs, criteria, step_s)
    gaps = coverage_gaps(session, specs, orientations, step_s)
    problems = operating_window_problems(session, criteria)

    # 한국어 Windows 콘솔(cp949)에서 죽지 않게 한다 (ppe_live_check 와 같은 가드).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    print(render(session, results, str(args.session), gaps, problems))
    return 0 if all(c.passed for c in results) and not gaps and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
