"""PPE 판정 파이프라인의 전처리·후처리 단계를 하나씩 끄거나 바꿔 3.7.3 지표를 다시 잰다.

같은 프레임·같은 라벨 구간(`session.json` 의 segment·expected·orientation)을 쓰고, 변형마다
`acceptance_judge` 의 C1~C3 와 `episode_eval` 의 에피소드 지표를 낸다. 로봇·카메라를 건드리지
않는 오프라인 계산이며, 런타임 기본 동작은 바꾸지 않는다 — 변형은 모두 이 도구 안의 매개변수다.

    # ① 프레임마다 추론을 한 번 돌려 원시 검출을 캐시한다 (모델·프레임 필요, 얼굴 프레임은 읽기만)
    python tools/ppe/ablation.py infer --frames <세션>/frames --models-dir models \\
        --cache <저장소 밖>/cache.json
    # ② 캐시와 세션 라벨로 변형별 표를 낸다 (모델 불필요)
    python tools/ppe/ablation.py report <세션>/session.json --cache <저장소 밖>/cache.json
    # ③ 모델만 바꿔 뽑은 캐시 여러 개를 base 설정으로 맞대어 센다 (모델 불필요)
    python tools/ppe/ablation.py compare <세션>/session.json --plan <세션>/plan.json \\
        --cache v4=<저장소 밖>/cache_v4.json --cache v5=<저장소 밖>/cache_v5.json

캐시
    사람 검출(COCO, 설정 conf)은 한 번, PPE 검출은 크롭 여유(`--pads`)·축소 배율(`--scales`)마다
    한 번, 사람 게이팅을 끈 풀프레임 한 번 돌린다. PPE 검출은 `--conf-floor`(기본 0.1)까지
    남겨 두므로 conf 임계값 변형은 다시 추론하지 않는다 — 클래스별 NMS 는 점수 내림차순 탐욕
    억제라 «억제 뒤 임계값» 과 «임계값 뒤 억제» 가 같은 박스를 남긴다.

변형 축
    gate          False 면 사람 크롭 대신 풀프레임 PPE 검출로 프레임당 판정 하나
    crop          캐시 키 — 크롭 여유 `0.08`, 축소 배율을 붙이면 `0.08@0.5`
    conf          PPE 검출 conf 임계값
    clip          crop  = `ppe_live_check.judge` 와 같다 — 머리 박스 상단을 **크롭 좌표**로 잰다
                  frame = `PpeDetector` 와 같다 — 사람 박스 상단·머리 상단을 **프레임 좌표**로 잰다
                  off   = 머리 클리핑 규칙을 끈다
    primary_only  가장 큰 사람 하나만 판정한다(런타임 `PpeDetector` 와 같다)
    window        live  = `ppe_live_check.ViolationWindow` — 시간 창, 창이 비어야 해제
                  clear = `PpeDetector` — 적합 판정 `clear_after_ok` 회 연속에 누적을 지운다
                  (1 이면 런타임 그대로, 2 이상이면 hysteresis). 확인불가는 연속을 끊지도 세지도
                  않는다. 사람이 없으면 비운다.
    hits_required 위반 확정 히트 수. 1 이면 연속 프레임 누적(투표)을 끈 것이다.

창은 세션 도구처럼 구간이 바뀔 때 비운다. 시각은 기록된 `t`(0.1초 반올림)를 쓴다.

합격 기준이 없는 계획 (예: 위반 구간만 찍은 후면 세션)
    C1~C3 칸은 `-` 로 두고 표 위에 한 줄로 밝힌다. 판정 가능률·실효 성공률의 분모는 계획에
    있고 세션에도 있는 라벨 구간이다(기준이 있으면 `judged_specs` 구간만). 계획에 없는 구간은
    프레임 지표에서 빠지지만, 에피소드 지표는 계획을 보지 않으므로 그 구간도 센다. 정상
    에피소드가 없으면 오경보율은 `-` 다.

클리핑 제외 실효 성공률 (표 끝 열)
    그 변형이 스스로 `머리 클리핑` 사유로 낸 확인불가를 분모에서 뺀 실효 성공률 —
    `compare` 표의 같은 이름 열과 같은 정의다.

단계 누적 (`STAGES`, 캐시가 있을 때)
    한 축씩 끄는 변형과 달리 Raw 검출기(풀프레임·클리핑 끔·누적 끔)에서 사람 크롭 → 머리
    클리핑 필터 → 시간 누적을 차례로 쌓는다. 마지막 단계는 base 와 같다.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.vision.detector import Detection  # noqa: E402
from tools.ppe import episode_eval  # noqa: E402
from tools.ppe.acceptance_judge import (  # noqa: E402
    Criterion,
    judge_session,
    judged_specs,
    load_criteria,
)
from tools.ppe.ppe_live_check import (  # noqa: E402
    DEFAULT_ACCEPTANCE_PLAN,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_VIOLATION,
    Judgement,
    ViolationWindow,
    crop_person,
    judge,
    load_acceptance_plan,
    verdict_tally,
)

NO_PEOPLE = "사람없음"
BASE_CROP = "0.08"


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    note: str = ""
    gate: bool = True
    crop: str = BASE_CROP
    conf: float = 0.5
    clip: str = "crop"
    primary_only: bool = False
    window: str = "live"
    clear_after_ok: int = 1
    hits_required: int = 3
    window_ms: int = 1500


BASE = Variant("base", "배포 설정 재현 (ppe_live_check 와 같은 경로)")

#: 추론 캐시가 있을 때 도는 변형. 한 번에 한 축만 바꾼다.
VARIANTS: tuple[Variant, ...] = (
    BASE,
    replace(BASE, name="gate-off", note="사람 게이팅 끔 — 풀프레임 PPE", gate=False),
    replace(BASE, name="clip-off", note="상단 닿음(클리핑) 규칙 끔", clip="off"),
    replace(
        BASE, name="clip-frame", note="클리핑을 프레임 좌표로 (런타임 PpeDetector)", clip="frame"
    ),
    replace(BASE, name="pad-0", note="크롭 여유 0", crop="0"),
    replace(BASE, name="pad-0.15", note="크롭 여유 0.15", crop="0.15"),
    replace(BASE, name="pad-0.25", note="크롭 여유 0.25", crop="0.25"),
    replace(BASE, name="half-res", note="크롭을 절반 해상도로 줄여 넣음", crop="0.08@0.5"),
    replace(
        BASE, name="primary-only", note="가장 큰 사람 하나만 (런타임과 같다)", primary_only=True
    ),
    replace(BASE, name="conf-0.3", note="PPE conf 0.3", conf=0.3),
    replace(BASE, name="conf-0.4", note="PPE conf 0.4", conf=0.4),
    replace(BASE, name="conf-0.6", note="PPE conf 0.6", conf=0.6),
    replace(BASE, name="conf-0.7", note="PPE conf 0.7", conf=0.7),
    replace(BASE, name="vote-off", note="누적 끔 — 위반 1회로 확정", hits_required=1),
    replace(BASE, name="clear-1", note="적합 1회에 누적 지움 (런타임)", window="clear"),
    replace(
        BASE,
        name="clear-3",
        note="hysteresis — 적합 3회 연속에 지움",
        window="clear",
        clear_after_ok=3,
    ),
    replace(
        BASE,
        name="clear-5",
        note="hysteresis — 적합 5회 연속에 지움",
        window="clear",
        clear_after_ok=5,
    ),
)

#: 계획서 2단계 표 — Raw 검출기에서 단계를 하나씩 쌓는다. 앞 단계와 한 축씩만 다르고 끝은 base 다.
STAGES: tuple[Variant, ...] = (
    replace(
        BASE,
        name="stage-raw",
        note="Raw — 풀프레임 PPE, 클리핑 끔, 누적 끔",
        gate=False,
        clip="off",
        hits_required=1,
    ),
    replace(BASE, name="stage-crop", note="+ 사람 크롭", clip="off", hits_required=1),
    replace(BASE, name="stage-clip", note="+ 머리 클리핑 필터", hits_required=1),
    replace(BASE, name="stage-vote", note="+ 시간 누적 (= base)"),
)

#: 기록된 판정만 다시 누적하는 변형 (추론 없이, 프레임 오염과 무관). 누적 규칙 축만 의미가 있다.
WINDOW_VARIANTS: tuple[Variant, ...] = tuple(
    v for v in VARIANTS if v.name in ("base", "vote-off", "clear-1", "clear-3", "clear-5")
)


# ── 판정 ───────────────────────────────────────────────────


def _detections(cached: Iterable[Sequence[Any]], conf: float) -> list[Detection]:
    return [
        Detection(str(d[0]), float(d[1]), (float(d[2]), float(d[3]), float(d[4]), float(d[5])))
        for d in cached
        if float(d[1]) >= conf
    ]


def judge_person(
    cached: Sequence[Sequence[Any]] | None,
    *,
    conf: float,
    clip: str,
    head_margin: int,
    origin: tuple[int, int],
    person_box: Sequence[float] | None,
) -> Judgement:
    """캐시된 PPE 검출(크롭 좌표) 하나 → 판정. `clip` 은 모듈 설명을 본다."""
    if cached is None:
        return Judgement(STATE_UNKNOWN, "크롭 실패", ())
    found = _detections(cached, conf)
    if clip == "off":
        return judge(found, head_margin, False)
    if clip == "crop":
        return judge(found, head_margin, True)
    if clip != "frame":
        raise ValueError(f"모르는 clip 모드: {clip!r}")
    if person_box is not None and person_box[1] <= head_margin:
        return Judgement(STATE_UNKNOWN, "머리 클리핑", tuple(found))
    ox, oy = origin
    shifted = [
        Detection(d.label, d.score, (d.box[0] + ox, d.box[1] + oy, d.box[2] + ox, d.box[3] + oy))
        for d in found
    ]
    return judge(shifted, head_margin, True)


def frame_judgements(frame: dict[str, Any], variant: Variant, head_margin: int) -> list[Judgement]:
    """캐시된 프레임 하나 → 사람별 판정 (게이팅을 끄면 프레임당 하나)."""
    if not variant.gate:
        return [
            judge_person(
                frame["full"],
                conf=variant.conf,
                clip=variant.clip,
                head_margin=head_margin,
                origin=(0, 0),
                person_box=None,
            )
        ]
    persons = frame["persons"]
    if not persons:
        return []
    crops = frame["crops"].get(variant.crop)
    if crops is None:
        raise ValueError(f"캐시에 크롭 {variant.crop!r} 가 없다 — infer 의 --pads/--scales 를 본다")
    indices: Sequence[int] = range(len(persons))
    if variant.primary_only:
        indices = [max(indices, key=lambda i: persons[i]["box"][3] - persons[i]["box"][1])]
    out = []
    for i in indices:
        entry = crops[i]
        out.append(
            judge_person(
                entry["dets"] if entry else None,
                conf=variant.conf,
                clip=variant.clip,
                head_margin=head_margin,
                origin=tuple(entry["origin"]) if entry else (0, 0),  # type: ignore[arg-type]
                person_box=persons[i]["box"],
            )
        )
    return out


# ── 누적 규칙 ───────────────────────────────────────────────


class LiveWindow:
    """`ppe_live_check.ViolationWindow` 를 판정 목록으로 부르는 얇은 감싸개."""

    def __init__(self, window_ms: int, hits_required: int) -> None:
        self._window = ViolationWindow(window_ms, hits_required)

    def observe(self, states: Sequence[str], now_ms: int) -> bool:
        return self._window.observe(STATE_VIOLATION in states, now_ms)

    @property
    def hits(self) -> int:
        return self._window.hits

    def reset(self) -> None:
        self._window.reset()


class ClearingWindow:
    """`PpeDetector` 의 누적 — 적합이 `clear_after_ok` 회 연속이면 지운다. 상승 에지를 낸다."""

    def __init__(self, window_ms: int, hits_required: int, clear_after_ok: int = 1) -> None:
        self._window = int(window_ms)
        self._required = int(hits_required)
        self._clear_after = int(clear_after_ok)
        self._hits: collections.deque[int] = collections.deque()
        self._ok_run = 0
        self._level = False

    def observe(self, states: Sequence[str], now_ms: int) -> bool:
        if not states:
            self.reset()
            return False
        if STATE_VIOLATION in states:
            self._hits.append(now_ms)
            self._ok_run = 0
        elif STATE_OK in states:
            self._ok_run += 1
            if self._ok_run >= self._clear_after:
                self._hits.clear()
        while self._hits and now_ms - self._hits[0] > self._window:
            self._hits.popleft()
        level = len(self._hits) >= self._required
        newly = level and not self._level
        self._level = level
        return newly

    @property
    def hits(self) -> int:
        return len(self._hits)

    def reset(self) -> None:
        self._hits.clear()
        self._ok_run = 0
        self._level = False


def make_window(variant: Variant) -> LiveWindow | ClearingWindow:
    if variant.window == "live":
        return LiveWindow(variant.window_ms, variant.hits_required)
    if variant.window == "clear":
        return ClearingWindow(variant.window_ms, variant.hits_required, variant.clear_after_ok)
    raise ValueError(f"모르는 window 모드: {variant.window!r}")


# ── 재생 ───────────────────────────────────────────────────


def rebuild_segments(events: Sequence[dict[str, Any]], orientations: Sequence[str]) -> dict:
    """이벤트 → `SegmentLog.snapshot()` 과 같은 모양의 구간 집계 (seconds 는 비운다)."""
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event.get("segment")
        if not key:
            continue
        slot = out.setdefault(
            key,
            {
                "verdicts": {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0},
                "orientations": {
                    name: {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0}
                    for name in orientations
                },
                "frames": 0,
            },
        )
        slot["frames"] += 1
        facing = event.get("orientation")
        for state in event.get("states", []):
            slot["verdicts"][state] = slot["verdicts"].get(state, 0) + 1
            if facing:
                slot["orientations"][facing][state] = slot["orientations"][facing].get(state, 0) + 1
    return out


def _assemble(
    labels: Sequence[dict[str, Any]],
    judged: Sequence[list[Judgement]],
    variant: Variant,
    orientations: Sequence[str],
    meta: dict[str, Any],
) -> dict[str, Any]:
    window = make_window(variant)
    counts: collections.Counter[str] = collections.Counter()
    reasons: collections.Counter[str] = collections.Counter()
    events: list[dict[str, Any]] = []
    segment = None
    for label, results in zip(labels, judged, strict=True):
        if label.get("segment") != segment:
            window.reset()
            segment = label.get("segment")
        states = [j.state for j in results]
        newly = window.observe(states, int(round(float(label["t"]) * 1000)))
        if not results:
            counts[NO_PEOPLE] += 1
        for j in results:
            counts[j.state] += 1
            if j.reason:
                reasons[j.reason] += 1
        events.append(
            {
                "t": label["t"],
                "tag": label["tag"],
                "people": len(results),
                "states": states,
                "reasons": [j.reason for j in results if j.reason],
                "labels": sorted({d.label for j in results for d in j.detections}),
                "confirmed": bool(newly),
                "hits": window.hits,
                "segment": label.get("segment"),
                "expected": label.get("expected"),
                "orientation": label.get("orientation"),
            }
        )
    return {
        **meta,
        "variant": variant.name,
        "counts": dict(counts),
        "reasons": dict(reasons),
        "settings": {
            **meta.get("settings", {}),
            "window_ms": variant.window_ms,
            "hits_required": variant.hits_required,
            # CLI 의 --window-ms/--hits 덮어쓰기 표시다. 변형의 창은 window_ms·hits_required 로
            # 이미 기록되고 judge 가 기준과 따로 비교하므로 여기서는 항상 False 다.
            "overridden": False,
        },
        "segments": rebuild_segments(events, orientations),
        "events": events,
    }


def replay(
    frames: Sequence[dict[str, Any]],
    labels: Sequence[dict[str, Any]],
    variant: Variant,
    head_margin: int,
    orientations: Sequence[str],
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """캐시된 검출 + 세션 라벨 → 변형으로 다시 판정한 세션."""
    by_tag = {f["tag"]: f for f in frames}
    judged = []
    for label in labels:
        frame = by_tag.get(label["tag"])
        if frame is None:
            raise ValueError(f"캐시에 프레임 {label['tag']} 가 없다 — 같은 세션의 캐시인지 본다")
        judged.append(frame_judgements(frame, variant, head_margin))
    return _assemble(labels, judged, variant, orientations, meta or {})


def _plan_orientations(plan: Path = DEFAULT_ACCEPTANCE_PLAN) -> list[str]:
    return list(json.loads(plan.read_text(encoding="utf-8"))["orientations"])


def replay_recorded(
    session: dict[str, Any], variant: Variant, orientations: Sequence[str] | None = None
) -> dict[str, Any]:
    """기록된 판정은 그대로 두고 누적 규칙만 바꿔 다시 센다 (추론 없음)."""
    judged = [
        [
            Judgement(state, reason, ())
            for state, reason in zip(
                e["states"],
                _align_reasons(e["states"], e.get("reasons", [])),
                strict=True,
            )
        ]
        for e in session["events"]
    ]
    meta = {k: v for k, v in session.items() if k not in ("events", "segments")}
    out = _assemble(session["events"], judged, variant, orientations or _plan_orientations(), meta)
    # 판정이 같으니 라벨 이름도 기록값을 그대로 둔다.
    for new, old in zip(out["events"], session["events"], strict=True):
        new["labels"] = old.get("labels", [])
    return out


def _align_reasons(states: Sequence[str], reasons: Sequence[str]) -> list[str]:
    """기록은 사유가 있는 판정의 사유만 적었다 — 확인불가 판정에 순서대로 되붙인다."""
    queue = list(reasons)
    return [queue.pop(0) if state == STATE_UNKNOWN and queue else "" for state in states]


# ── 지표 ───────────────────────────────────────────────────


def _expected_by_segment(plan: Path, scenario: str) -> dict[str, str]:
    specs, _, _ = load_acceptance_plan(plan, scenario)
    return {spec["key"]: spec["expected"] for spec in specs}


def _criteria_or_none(plan: Path, scenario: str) -> dict[str, Any] | None:
    """계획에 합격 기준이 아예 없으면 None. 있는데 잘못됐으면 `load_criteria` 처럼 거부한다."""
    data = json.loads(plan.read_text(encoding="utf-8"))
    if not data.get("scenarios", {}).get(scenario, {}).get("criteria"):
        return None
    return load_criteria(plan, scenario)


def evaluate_variant(
    session: dict[str, Any], plan: Path = DEFAULT_ACCEPTANCE_PLAN
) -> dict[str, Any]:
    """세션 하나 → 표 한 줄: 판정 가능률·실효 성공률·C1~C3·사유·에피소드 지표.

    계획에 합격 기준이 없으면 `criteria` 는 None 이고, 분모는 세션에 있는 라벨 구간 전체다.
    """
    scenario = session.get("scenario") or "xiao"
    specs, step_s, _ = load_acceptance_plan(plan, scenario)
    criteria = _criteria_or_none(plan, scenario)
    results: list[Criterion] | None = None
    if criteria is None:
        counted = [s for s in specs if s["key"] in session.get("segments", {})]
    else:
        results = judge_session(session, specs, criteria, step_s)
        counted = judged_specs(specs, criteria)

    right = total = determinate = covered = 0
    # C1·C3 와 같은 분모 — 판정 대상 구간만 센다 (기준이 없으면 세션의 라벨 구간 전체).
    for spec in counted:
        stats = session.get("segments", {}).get(spec["key"])
        if not stats:
            continue
        tally = verdict_tally(spec["expected"], stats["verdicts"])
        right += tally["right"]
        total += tally["count"]
        determinate += tally["coverage_determinate"]
        covered += tally["coverage_count"]

    summary = episode_eval.evaluate([("session", session)], timeout_s=float(step_s))
    ep = summary["total"]["episode"]
    # C2 는 앞 방향에서 이월된 경보도 확정으로 센다. 그 방향 에피소드 안에서 제한시간 안에
    # 스스로 낸 경보가 있는 방향만 따로 센다.
    judged = {spec["key"] for spec in counted}
    # 그 변형이 스스로 낸 머리 클리핑 확인불가 — 클리핑 제외 실효 성공률의 분모에서 뺀다.
    by_segment = segment_tally(session)
    clipped = sum(by_segment[key]["clipped"] for key in judged if key in by_segment)
    own: dict[str, set[str | None]] = {
        s["key"]: set() for s in specs if s["key"] in judged and s["expected"] == STATE_VIOLATION
    }
    for e in summary["episodes"]:
        if (
            e["segment"] in own
            and e["excluded"] is None
            and e["first_alarm_s"] is not None
            and e["first_alarm_s"] <= step_s + episode_eval.EPS
        ):
            own[e["segment"]].add(e["orientation"])
    return {
        "name": session.get("variant", ""),
        "determinate": determinate,
        "total": covered,
        "decidable_rate": determinate / covered if covered else None,
        "right": right,
        "count": total,
        "effective_rate": right / total if total else None,
        "clipped": clipped,
        "clip_excluded_rate": right / (total - clipped) if total - clipped else None,
        "criteria": results,
        "reasons": dict(session.get("reasons", {})),
        "alarms_total": sum(bool(e.get("confirmed")) for e in session.get("events", [])),
        "recall": ep["recall"],
        "detected": ep["detected"],
        "violation_episodes": ep["violation_episodes"],
        "false_alarm_rate": ep["false_alarm_rate"],
        "false_alarms": ep["false_alarms"],
        "normal_episodes": ep["normal_episodes"],
        "latency_p95_s": ep["latency"]["p95_s"],
        "latency_mean_s": ep["latency"]["mean_s"],
        "latency_n": ep["latency"]["n"],
        "carried": ep["carried"],
        "own_directions": {key: len(found) for key, found in own.items()},
    }


def breakdown(
    session: dict[str, Any], plan: Path = DEFAULT_ACCEPTANCE_PLAN
) -> list[dict[str, Any]]:
    """구간(착용 상태) × 방향별 프레임 기준 집계."""
    expected = _expected_by_segment(plan, session.get("scenario") or "xiao")
    rows = []
    for key, stats in session.get("segments", {}).items():
        for orientation, verdicts in stats["orientations"].items():
            tally = verdict_tally(expected[key], verdicts)
            if tally["count"]:
                rows.append({"segment": key, "orientation": orientation, **tally})
    return rows


def breakdown_by_people(
    session: dict[str, Any], plan: Path = DEFAULT_ACCEPTANCE_PLAN
) -> list[dict[str, Any]]:
    """구간 안 프레임을 화면 속 사람 수로 나눈 판정 집계 (사람 0명은 판정이 없어 뺀다)."""
    expected = _expected_by_segment(plan, session.get("scenario") or "xiao")
    groups: dict[int, dict[str, int]] = {}
    for event in session.get("events", []):
        key, people = event.get("segment"), int(event.get("people", 0))
        if not key or not people:
            continue
        verdicts = collections.Counter(event["states"])
        tally = verdict_tally(expected[key], dict(verdicts))
        slot = groups.setdefault(people, {})
        for name, value in tally.items():
            slot[name] = slot.get(name, 0) + value
        slot["frames"] = slot.get("frames", 0) + 1
    return [{"people": p, **groups[p]} for p in sorted(groups)]


def agreement(a: dict[str, Any], b: dict[str, Any]) -> float:
    """두 세션의 프레임별 판정 목록이 같은 비율."""
    pairs = list(zip(a["events"], b["events"], strict=True))
    return sum(x["states"] == y["states"] for x, y in pairs) / len(pairs) if pairs else 0.0


# ── 모델 비교 ───────────────────────────────────────────────

CLIP_REASON = "머리 클리핑"
STATE_ORDER = (STATE_OK, STATE_VIOLATION, STATE_UNKNOWN)


def segment_tally(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """구간별 사람 판정 집계. 머리 클리핑 확인불가는 `clipped` 로 따로 센다(`unknown` 에 포함).

    기대값은 이벤트에 기록된 `expected` 를 쓴다. 구간 밖 프레임은 뺀다.
    """
    out: dict[str, dict[str, Any]] = {}
    for event in session["events"]:
        key = event.get("segment")
        if not key:
            continue
        expected = event["expected"]
        slot = out.setdefault(
            key,
            {
                "expected": expected,
                "count": 0,
                "right": 0,
                "wrong": 0,
                "unknown": 0,
                "clipped": 0,
                "alarms": 0,
            },
        )
        states = event["states"]
        for state, reason in zip(
            states, _align_reasons(states, event.get("reasons", [])), strict=True
        ):
            slot["count"] += 1
            if state == STATE_UNKNOWN:
                slot["unknown"] += 1
                slot["clipped"] += reason == CLIP_REASON
            elif state == expected:
                slot["right"] += 1
            else:
                slot["wrong"] += 1
        slot["alarms"] += bool(event.get("confirmed"))
    return out


def paired_tally(
    a: dict[str, Any], b: dict[str, Any]
) -> dict[str, collections.Counter[tuple[str, str]]]:
    """같은 사람 검출로 판정한 두 세션을 사람 단위로 맞대어 (a 판정, b 판정) 쌍을 센다.

    사람 검출(COCO)이 같아야 사람 순서가 맞는다. 프레임 수·태그·구간·사람 수가 다르면 거부한다.
    """
    if len(a["events"]) != len(b["events"]):
        raise ValueError(
            f"프레임 수가 다르다: {len(a['events'])} ≠ {len(b['events'])} — 같은 세션인지 본다"
        )
    out: dict[str, collections.Counter[tuple[str, str]]] = {}
    for x, y in zip(a["events"], b["events"], strict=True):
        if x["tag"] != y["tag"]:
            raise ValueError(f"프레임 태그가 다르다: {x['tag']} ≠ {y['tag']} — 같은 세션인지 본다")
        if x.get("segment") != y.get("segment"):
            raise ValueError(
                f"프레임 {x['tag']} 의 구간이 다르다: {x.get('segment')} ≠ {y.get('segment')}"
                " — 같은 세션인지 본다"
            )
        if len(x["states"]) != len(y["states"]):
            raise ValueError(f"프레임 {x['tag']} 의 사람 수가 다르다 — 같은 사람 검출인지 본다")
        key = x.get("segment")
        if not key:
            continue
        out.setdefault(key, collections.Counter()).update(
            zip(x["states"], y["states"], strict=True)
        )
    return out


def compare_lines(
    session: dict[str, Any], replays: dict[str, dict[str, Any]], cache_meta: dict[str, Any]
) -> list[str]:
    """기록 세션과 모델별 base 재추론 → 구간 집계 표와 사람 단위 맞대기 표."""
    lines = ["## 모델별 구간 집계 (base 설정, 같은 사람 검출)", ""]
    for name, replayed in replays.items():
        meta = cache_meta[name]
        lines.append(
            f"- {name}: 기록 판정과의 프레임별 판정 일치율 {agreement(replayed, session):.1%}"
            f" · 프레임 {meta.get('frames', '-')} · PPE conf 하한 {meta.get('conf_floor', '-')}"
        )
    lines += [
        "",
        "| 구간 | 기대 | 모델 | 판정 | 일치 | 불일치 | 확인불가 | 머리 클리핑 | 판정 가능률 "
        "| 실효 성공률 | 클리핑 제외 실효 성공률 | 확정 경보 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    tallies = {"기록": segment_tally(session)}
    tallies.update({name: segment_tally(replayed) for name, replayed in replays.items()})
    for key in tallies["기록"]:
        for name, tally in tallies.items():
            r = tally[key]
            unclipped = r["count"] - r["clipped"]
            lines.append(
                f"| {key} | {r['expected']} | {name} | {r['count']} | {r['right']} | "
                f"{r['wrong']} | {r['unknown']} | {r['clipped']} | "
                f"{_pct((r['right'] + r['wrong']) / r['count'] if r['count'] else None)} | "
                f"{_pct(r['right'] / r['count'] if r['count'] else None)} | "
                f"{_pct(r['right'] / unclipped if unclipped else None)} | {r['alarms']} |"
            )
    names = list(replays)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            pairs = paired_tally(replays[first], replays[second])
            lines += [
                "",
                f"## 사람 단위 맞대기 ({first} 판정 → {second} 판정)",
                "",
                f"| 구간 | {first} 판정 | "
                + " | ".join(f"{second} {state}" for state in STATE_ORDER)
                + " |",
                "|---|---|---|---|---|",
            ]
            for key, counter in pairs.items():
                for row in STATE_ORDER:
                    cells = " | ".join(str(counter.get((row, col), 0)) for col in STATE_ORDER)
                    lines.append(f"| {key} | {row} | {cells} |")
    return lines


def _named_path(text: str) -> tuple[str, str]:
    name, sep, path = text.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"이름=경로 꼴이어야 한다: {text!r}")
    return name, path


def run_compare(args: argparse.Namespace) -> int:
    session = json.loads(Path(args.session).read_text(encoding="utf-8"))
    orientations = _plan_orientations(Path(args.plan))
    meta = {k: v for k, v in session.items() if k not in ("events", "segments")}
    replays: dict[str, dict[str, Any]] = {}
    cache_meta: dict[str, dict[str, Any]] = {}
    for name, path in args.cache:
        cache = json.loads(Path(path).read_text(encoding="utf-8"))
        _require_base_conf(name, cache)
        cache_meta[name] = {"frames": len(cache["frames"]), **cache.get("meta", {})}
        replays[name] = replay(
            cache["frames"], session["events"], BASE, int(args.head_margin), orientations, meta
        )
    _write_text("\n".join(compare_lines(session, replays, cache_meta)) + "\n", args.out)
    return 0


def _require_base_conf(name: str, cache: dict[str, Any]) -> None:
    """base 재추론에는 conf 0.5 까지의 검출이 필요하다 — 캐시 하한이 더 높으면 거부한다."""
    floor = cache.get("meta", {}).get("conf_floor")
    if floor is not None and floor > BASE.conf + 1e-9:
        raise SystemExit(
            f"캐시 {name} 의 PPE conf 하한 {floor} 이 base conf {BASE.conf} 보다 높다"
            " — 하한 이하로 다시 infer 한다"
        )


def _write_text(text: str, out: str | None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(f"표 {out}")
    else:
        print(text)


# ── 추론 캐시 ───────────────────────────────────────────────


def _encode(found: Iterable[Detection], scale: float = 1.0) -> list[list[Any]]:
    return [[d.label, round(d.score, 4), *(round(v / scale, 2) for v in d.box)] for d in found]


def infer_frame(
    image: np.ndarray,
    tag: str,
    coco: Any,
    ppe: Any,
    *,
    person_label: str,
    pads: Sequence[float],
    scales: Sequence[float] = (),
    full: bool = True,
) -> dict[str, Any]:
    """프레임 하나 → 캐시 레코드. 박스는 크롭 좌표(축소분은 되돌림)다."""
    import cv2

    persons = [d for d in coco.detect(image) if d.label == person_label]
    crops: dict[str, list[dict[str, Any] | None]] = {}
    for pad in pads:
        plain: list[dict[str, Any] | None] = []
        scaled: dict[float, list[dict[str, Any] | None]] = {s: [] for s in scales}
        for person in persons:
            crop, origin = crop_person(image, person.box, pad)
            if crop is None:
                plain.append(None)
                for s in scales:
                    scaled[s].append(None)
                continue
            plain.append(
                {"origin": [int(origin[0]), int(origin[1])], "dets": _encode(ppe.detect(crop))}
            )
            height, width = crop.shape[:2]
            for s in scales:
                small = cv2.resize(
                    crop,
                    (max(1, int(width * s)), max(1, int(height * s))),
                    interpolation=cv2.INTER_AREA,
                )
                scaled[s].append(
                    {
                        "origin": [int(origin[0]), int(origin[1])],
                        "dets": _encode(ppe.detect(small), s),
                    }
                )
        crops[f"{pad:g}"] = plain
        for s in scales:
            crops[f"{pad:g}@{s:g}"] = scaled[s]
    return {
        "tag": tag,
        "shape": [int(image.shape[0]), int(image.shape[1])],
        "persons": [
            {"score": round(p.score, 4), "box": [round(v, 2) for v in p.box]} for p in persons
        ],
        "crops": crops,
        "full": _encode(ppe.detect(image)) if full else [],
    }


def run_infer(args: argparse.Namespace) -> int:
    import copy

    import cv2

    from host.common.config import load_config
    from host.vision.coco_labels import COCO_CLASSES
    from host.vision.detector import Detector

    config = copy.deepcopy(load_config(args.device))
    models = Path(args.models_dir).resolve()
    config["vision"]["coco"]["model_path"] = str(models / "coco.onnx")
    config["vision"]["ppe"]["model_path"] = str(models / "ppe.onnx")
    config["vision"]["ppe"]["conf_threshold"] = float(args.conf_floor)
    coco = Detector(config, section="coco", labels=COCO_CLASSES)
    ppe = Detector(config, section="ppe", labels=list(config["vision"]["ppe"]["classes"]))
    coco.open()
    ppe.open()

    files = sorted(p for p in Path(args.frames).iterdir() if p.suffix.lower() == ".jpg")
    if args.limit:
        files = files[: args.limit]
    records = []
    for index, path in enumerate(files, start=1):
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            continue
        records.append(
            infer_frame(
                image,
                path.stem,
                coco,
                ppe,
                person_label=config["vision"]["coco"]["person_class"],
                pads=args.pads,
                scales=args.scales,
            )
        )
        if index % 500 == 0:
            print(f"  {index}/{len(files)}", flush=True)
    payload = {
        "meta": {
            "conf_floor": float(args.conf_floor),
            "coco_conf": float(config["vision"]["coco"]["conf_threshold"]),
            "pads": list(args.pads),
            "scales": list(args.scales),
            "frames": len(records),
        },
        "frames": records,
    }
    out = Path(args.cache)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"캐시 {out} · 프레임 {len(records)}장")
    return 0


# ── 출력 ───────────────────────────────────────────────────


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _sec(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


REASON_ORDER = ("머리 미검출", "아무것도 미검출", "머리 클리핑", "몸통 미검출", "크롭 실패")


def table(rows: Sequence[dict[str, Any]], notes: dict[str, str]) -> list[str]:
    header = [
        "변형",
        "내용",
        "판정 가능률",
        "실효 성공률",
        "C1 경보",
        "C2 방향(이월)",
        "자체 확정 방향",
        "C3",
        "recall",
        "오경보율",
        "판정시간 P95(n)",
        *REASON_ORDER,
        "클리핑 제외 실효 성공률",
    ]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    if any(r["criteria"] is None for r in rows):
        lines = [
            "- 합격 기준 없음: C1~C3 는 `-`, 판정 가능률·실효 성공률의 분모는 세션의 라벨 구간"
            " 전체, 정상 에피소드가 없으면 오경보율은 `-`",
            "",
            *lines,
        ]
    for r in rows:
        if r["criteria"] is None:
            c1_cell = c2_cell = c3_cell = "-"
        else:
            c1, c2, c3 = r["criteria"]
            c1_cell = ("PASS " if c1.passed else "FAIL ") + c1.detail.split(" — ")[0]
            c2_cell = ("PASS " if c2.passed else "FAIL ") + _c2_short(c2.detail)
            c3_cell = "PASS" if c3.passed else "FAIL"
        false_alarm = (
            f"{_pct(r['false_alarm_rate'])} ({r['false_alarms']}/{r['normal_episodes']})"
            if r["normal_episodes"]
            else "-"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    r["name"],
                    notes.get(r["name"], ""),
                    f"{_pct(r['decidable_rate'])} ({r['determinate']}/{r['total']})",
                    f"{_pct(r['effective_rate'])} ({r['right']}/{r['count']})",
                    c1_cell,
                    c2_cell,
                    " · ".join(str(n) for n in r["own_directions"].values()),
                    c3_cell,
                    f"{_pct(r['recall'])} ({r['detected']}/{r['violation_episodes']})",
                    false_alarm,
                    f"{_sec(r['latency_p95_s'])}초 ({r['latency_n']})",
                    *(str(r["reasons"].get(k, 0)) for k in REASON_ORDER),
                    f"{_pct(r['clip_excluded_rate'])} ({r['right']}/{r['count'] - r['clipped']})",
                ]
            )
            + " |"
        )
    return lines


def _c2_short(detail: str) -> str:
    body = detail.split(" — ")[0].split(" (하한")[0]
    return " · ".join(part.split(" ", 1)[1] for part in body.split(", ") if " " in part)


def breakdown_table(session: dict[str, Any], plan: Path) -> list[str]:
    lines = [
        "| 구간 | 방향 | 판정 | 기대 일치 | 불일치 | 확인불가 | 판정 가능률 | 실효 성공률 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in breakdown(session, plan):
        lines.append(
            f"| {r['segment']} | {r['orientation']} | {r['count']} | {r['right']} | {r['wrong']} | "
            f"{r['unknown']} | {_pct(r['determinate'] / r['count'])} | "
            f"{_pct(r['right'] / r['count'])} |"
        )
    lines += [
        "",
        "| 화면 속 사람 수 | 프레임 | 판정 | 기대 일치 | 확인불가 | 판정 가능률 | 실효 성공률 |",
    ]
    lines.append("|---|---|---|---|---|---|---|")
    for r in breakdown_by_people(session, plan):
        lines.append(
            f"| {r['people']} | {r['frames']} | {r['count']} | {r['right']} | {r['unknown']} | "
            f"{_pct(r['determinate'] / r['count'])} | {_pct(r['right'] / r['count'])} |"
        )
    return lines


def run_report(args: argparse.Namespace) -> int:
    session = json.loads(Path(args.session).read_text(encoding="utf-8"))
    plan = Path(args.plan)
    orientations = _plan_orientations(plan)
    cache: dict[str, Any] | None = None
    if args.cache:
        cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
        _require_base_conf(args.cache, cache)
    notes = {v.name: v.note for v in VARIANTS}
    lines = ["## 기록된 판정에 누적 규칙만 바꿔 다시 센 결과 (추론 없음)", ""]
    recorded_rows = []
    for variant in WINDOW_VARIANTS:
        row = evaluate_variant(replay_recorded(session, variant, orientations), plan)
        row["name"] = variant.name
        recorded_rows.append(row)
    lines += table(recorded_rows, notes)
    lines += ["", "### 조건별 분해 — 기록값", ""]
    lines += breakdown_table(session, plan)

    if cache is not None:
        meta = {k: v for k, v in session.items() if k not in ("events", "segments")}
        head_margin = int(args.head_margin)
        floor = cache.get("meta", {}).get("conf_floor", 0.0)
        rows = []
        replays: dict[str, dict[str, Any]] = {}
        for variant in VARIANTS:
            if variant.conf < floor - 1e-9:
                continue
            replayed = replay(
                cache["frames"], session["events"], variant, head_margin, orientations, meta
            )
            replays[variant.name] = replayed
            row = evaluate_variant(replayed, plan)
            row["name"] = variant.name
            rows.append(row)
        base = replays["base"]
        lines += [
            "",
            "## 다시 추론한 결과 (캐시)",
            "",
            f"- 기록 판정과 base 재추론의 프레임별 판정 일치율 {agreement(base, session):.1%}",
            "",
        ]
        lines += table(rows, notes)
        lines += ["", "### 조건별 분해 — base 재추론", ""]
        lines += breakdown_table(base, plan)

        stage_rows = []
        for variant in STAGES:
            row = evaluate_variant(
                replay(
                    cache["frames"], session["events"], variant, head_margin, orientations, meta
                ),
                plan,
            )
            row["name"] = variant.name
            stage_rows.append(row)
        lines += ["", "## 단계 누적 (계획서 2단계 표)", ""]
        lines += table(stage_rows, {v.name: v.note for v in STAGES})
    _write_text("\n".join(lines) + "\n", args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PPE 전처리·후처리 ablation")
    sub = parser.add_subparsers(dest="command", required=True)

    infer = sub.add_parser("infer", help="프레임마다 원시 검출을 캐시한다 (모델 필요)")
    infer.add_argument("--frames", required=True, help="세션 프레임 폴더 (얼굴 포함 — 읽기만)")
    infer.add_argument("--models-dir", required=True, help="coco.onnx · ppe.onnx 가 있는 폴더")
    infer.add_argument("--cache", required=True, help="캐시 JSON 경로 (저장소 밖 권장)")
    infer.add_argument("--device", default="mechdog-01")
    infer.add_argument("--pads", type=float, nargs="+", default=[0.0, 0.08, 0.15, 0.25])
    infer.add_argument("--scales", type=float, nargs="*", default=[])
    infer.add_argument("--conf-floor", type=float, default=0.1)
    infer.add_argument("--limit", type=int, help="앞에서부터 이 장수만 (점검용)")

    report = sub.add_parser("report", help="변형별 표를 낸다 (모델 불필요)")
    report.add_argument("session", help="ppe_live_check 의 session.json (라벨 구간)")
    report.add_argument("--cache", help="infer 캐시. 없으면 누적 규칙 변형만 낸다")
    report.add_argument("--plan", default=str(DEFAULT_ACCEPTANCE_PLAN))
    report.add_argument("--head-margin", type=int, default=8)
    report.add_argument("--out", help="Markdown 표를 쓸 파일")

    compare = sub.add_parser(
        "compare", help="모델별 캐시를 base 설정으로 맞대어 센다 (모델 불필요)"
    )
    compare.add_argument("session", help="ppe_live_check 의 session.json (라벨 구간)")
    compare.add_argument(
        "--cache",
        type=_named_path,
        action="append",
        required=True,
        metavar="이름=경로",
        help="같은 프레임·같은 COCO 로 뽑은 infer 캐시. 두 개 이상이면 사람 단위로 맞댄다",
    )
    compare.add_argument("--plan", default=str(DEFAULT_ACCEPTANCE_PLAN), help="방향 목록용 계획")
    compare.add_argument("--head-margin", type=int, default=8)
    compare.add_argument("--out", help="Markdown 표를 쓸 파일")

    args = parser.parse_args(argv)
    commands = {"infer": run_infer, "report": run_report, "compare": run_compare}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
