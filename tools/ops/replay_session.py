"""기록 세션 재생 — `--record-dir` 기록을 `Runtime` 에 다시 넣어 같은 판단이 나오는지 본다.

    python tools/ops/replay_session.py <기록 폴더> [--set network.cmd_rate_hz=5] [--json 보고.json]

기록(`host/telemetry/session_recorder.py`)의 입력 쪽 사건을 기록 시각 그대로 다시 넣는다.

| 기록 사건 | 재생에서 하는 일 |
| :--- | :--- |
| `runtime_begin` | 가짜 시계를 이 시각에 두고 `Runtime.begin` (세션 개시 전문) |
| `telemetry` | 그 시각에 `Runtime.receive` 원본 전문 그대로 |
| `vision` | 그 시각부터 가짜 비전 워커의 `latest()` 가 같은 판정을 내놓는다 |
| `operator` | 관제·콘솔 입력(`ask_patrol`·`apply_external`·`set_mode` …)을 같은 시각에 다시 부른다 |

출력(재생한 `command_sent`·`fsm`·`escalation`)을 기록의 같은 사건과 맞춰 본다. 명령은
`seq`·`ts` 를 빼고 비교한다 — 그 둘은 시계와 송신 횟수가 정하는 값이라 판단이 아니다.

⚠️ **아무것도 내보내지 않는다.** 소켓은 가짜이고, 블랙박스·대시보드·방송·ODOM·포즈 송신은
붙이지 않으며, VLM 은 «판독기 없음» 으로 둔다. 영상 프레임 파일도 열지 않는다.

재생하지 않는 것 — 보고서의 `limitations` 에 그 세션에 해당하는 것만 적는다:
LiDAR 길 찾기(`scan` 은 넣지 않는다), VLM 판독 결과, 대시보드가 `Commander` 를 직접 만진
조종(입력 기록이 없다), VLM 적재 대기 시간.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import hashlib
import json
import logging
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.behavior.fsm import Event  # noqa: E402
from host.behavior.mission import Mission  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.runtime import Runtime, build_parser  # noqa: E402
from host.telemetry.session_recorder import (  # noqa: E402
    EVENTS_FILE,
    MANIFEST_FILE,
    SUMMARY_FILE,
)
from host.vision.badge import Marker  # noqa: E402
from host.vision.detector import Detection  # noqa: E402
from host.vision.hazard_detector import HazardVerdict  # noqa: E402
from host.vision.person import FallenVerdict, Sighting  # noqa: E402
from host.vision.ppe_detector import PpeVerdict  # noqa: E402
from host.vision.tracker import Track  # noqa: E402
from host.vision.vlm_reader import VlmReader  # noqa: E402
from host.vision.worker import VisionResult  # noqa: E402

#: 명령 비교에서 빼는 필드 — 판단이 아니라 시계·송신 횟수가 정한다.
VOLATILE_FIELDS = ("seq", "ts")
#: 같은 시각에 다시 부르는 관제 입력 중 인자가 없는 것.
_PLAIN_ACTIONS = ("ask_patrol", "ask_reset", "ask_alarm_confirm", "send_emergency_stop")
#: 재생기가 다시 부를 수 있는 관제 입력 전체 — `Runtime._record_input` 이 남기는 이름과 같아야
#: 한다(시험이 런타임 소스와 맞춰 본다). 여기 없는 입력은 재생에서 빠지고 `limitations` 에 남는다.
OPERATOR_ACTIONS = frozenset(
    (
        *_PLAIN_ACTIONS,
        "apply_external",
        "set_mode",
        "ask_goto",
        "ask_locate_zone",
        "note_voice_listening",
        "note_voice_auth",
        "send_immediate",
    )
)


# ── 읽기 ─────────────────────────────────────────────────────
def load_session(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """manifest 와 사건 목록. 사건은 시각 순(같은 시각은 기록 순)으로 정렬한다."""
    manifest_path = directory / MANIFEST_FILE
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    )
    events = []
    with (directory / EVENTS_FILE).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e["t"])
    return manifest, events


def load_summary(directory: Path) -> dict[str, Any] | None:
    """기록기가 닫을 때 남긴 summary.json. 없으면 None — 기록이 정상 종료되지 않았다.

    있는데 읽지 못하면(반쯤 쓰였거나 JSON 객체가 아님) `{"read_error": 이유}` — «없음» 과 구별한다.
    """
    path = directory / SUMMARY_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # JSONDecodeError·UnicodeDecodeError 는 ValueError
        return {"read_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"read_error": f"JSON 객체가 아니다({type(data).__name__})"}
    return cast("dict[str, Any]", data)


def manifest_begin_ms(manifest: Mapping[str, Any]) -> int | None:
    """기록 시작 시각. 10-06 이전 기록은 같은 벽시계 값을 `started_mono_ms` 로 남겼다."""
    value = manifest.get("started_clock_ms", manifest.get("started_mono_ms"))
    return None if value is None else int(value)


def session_start_ms(events: Sequence[Mapping[str, Any]], begin_ms: int | None) -> int:
    """재생을 시작하는 시각 — 첫 `runtime_begin`, 없으면 manifest 시작, 그것도 없으면 첫 사건.

    `events` 는 시각 순이어야 한다. 보고의 «기동 기준» 표시도 이 값을 쓴다.
    """
    for event in events:
        if event["kind"] == "runtime_begin":
            return int(event["t"])
    if begin_ms is not None:
        return begin_ms
    return int(events[0]["t"]) if events else 0


def normalize_command(line: str) -> str:
    """`seq`·`ts` 를 뺀 정렬 JSON. JSON 이 아니면 그대로."""
    try:
        payload = json.loads(line)
    except ValueError:
        return line
    if not isinstance(payload, dict):
        return line
    for key in VOLATILE_FIELDS:
        payload.pop(key, None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── 영상 판정 되살리기 ───────────────────────────────────────
def _box(value: Sequence[float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = (float(v) for v in value)
    return (x1, y1, x2, y2)


def _detection(item: Mapping[str, Any]) -> Detection:
    return Detection(str(item["label"]), float(item["score"]), _box(item["box"]))


def vision_from_event(event: Mapping[str, Any]) -> tuple[VisionResult, bool]:
    """기록의 `vision` 사건으로 `VisionResult` 를 다시 만든다. 둘째 값은 «판정 전체가 있었나».

    판정 전체(`sighting`·`tracks`·`fallen` …)가 없는 옛 기록은 검출만으로 사람 판정을 근사한다
    — 그때 추적·쓰러짐·사원증·PPE 는 비어 있다.
    """
    detections = tuple(_detection(d) for d in event.get("detections", []))
    width, height = (int(v) for v in event.get("size", [0, 0]))
    exact = "sighting" in event
    if exact:
        s = event["sighting"]
        sighting = Sighting(
            present=bool(s["present"]),
            changed=bool(s["changed"]),
            hits=int(s["hits"]),
            best_score=float(s["best_score"]),
            last_seen_ms=s["last_seen_ms"],
            box=None if s["box"] is None else _box(s["box"]),
        )
        tracks = tuple(
            Track(
                track_id=int(t["track_id"]),
                box=_box(t["box"]),
                score=float(t["score"]),
                last_seen_ms=int(t["last_seen_ms"]),
            )
            for t in event.get("tracks", [])
        )
        f = event["fallen"]
        fallen = FallenVerdict(
            fallen=bool(f["fallen"]),
            changed=bool(f["changed"]),
            candidate=bool(f["candidate"]),
            aspect=f["aspect"],
            still_ms=int(f["still_ms"]),
        )
        markers = tuple(
            Marker(
                marker_id=int(m["marker_id"]), center=(float(m["center"][0]), float(m["center"][1]))
            )
            for m in event.get("markers", [])
        )
        p = event.get("ppe_verdict")
        if p is None and isinstance(event.get("ppe"), dict):
            p = event["ppe"]  # 예전 기록: 대표 판정 하나를 `ppe` 에 남겼다
        ppe = (
            None
            if p is None
            else PpeVerdict(
                track_id=int(p["track_id"]),
                state=str(p["state"]),
                reason=str(p.get("reason", "")),
                confirmed=bool(p.get("confirmed", False)),
                clipped=bool(p.get("clipped", False)),
                required=tuple(p.get("required", ("helmet", "vest"))),
            )
        )
        h = event.get("hazard")
        hazard = (
            None
            if h is None
            else HazardVerdict(
                detections=tuple(_detection(d) for d in h.get("detections", [])),
                confirmed=tuple(h.get("confirmed", ())),
            )
        )
    else:
        people = [d for d in detections if d.label == "person"]
        best = max(people, key=lambda d: d.score) if people else None
        sighting = Sighting(
            present=bool(event.get("person_present", False)),
            changed=False,
            hits=len(people),
            best_score=0.0 if best is None else best.score,
            last_seen_ms=int(event["t"]) if people else None,
            box=None if best is None else best.box,
        )
        tracks = ()
        fallen = FallenVerdict(
            fallen=False, changed=False, candidate=False, aspect=None, still_ms=0
        )
        markers = ()
        ppe = None
        hazard = None
    result = VisionResult(
        detections=detections,
        jpeg=b"",  # 프레임은 열지 않는다 — 얼굴이 찍혀 있고, 판단은 판정만 본다
        frame_seq=int(event["frame_seq"]),
        frame_width=width,
        frame_height=height,
        frame_received_ms=int(event["frame_received_ms"]),
        completed_ms=int(event["completed_ms"]),
        inference_ms=float(event.get("inference_ms", 0.0)),
        sighting=sighting,
        tracks=tracks,
        fallen=fallen,
        markers=markers,
        ppe=ppe,
        hazard=hazard,
    )
    return result, exact


# ── 가짜 부품 ────────────────────────────────────────────────
class ReplayClock:
    """재생 시계. `system_clock_ms` 자리에 꽂힌다."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class RecordedVision:
    """정해진 시각부터 정해진 결과를 내놓는 비전 워커. 단절 판정은 `VisionWorker` 와 같다."""

    def __init__(
        self,
        results: Sequence[tuple[int, VisionResult]],
        *,
        clock: Callable[[], int],
        stall_ms: int,
        hazard_available: bool = False,
    ) -> None:
        self._results = sorted(results, key=lambda r: r[0])
        self._clock = clock
        self._stall_ms = stall_ms
        self._started_ms: int | None = None
        self._hazard_available = hazard_available

    def start(self) -> None:
        self._started_ms = self._clock()

    def stop(self) -> None:
        pass

    def latest(self) -> VisionResult | None:
        now = self._clock()
        found = None
        for available_at, result in self._results:
            if available_at > now:
                break
            found = result
        return found

    def age_ms(self, now_ms: int) -> int | None:
        result = self.latest()
        return None if result is None else max(0, now_ms - result.completed_ms)

    def stalled(self, now_ms: int) -> bool:
        age = self.age_ms(now_ms)
        if age is not None:
            return age > self._stall_ms
        return self._started_ms is not None and now_ms - self._started_ms > self._stall_ms

    def healthy(self) -> bool:
        return True

    def set_ppe_enabled(self, enabled: bool) -> None:
        pass

    def set_ppe_requirements(self, required: tuple[str, ...]) -> None:
        pass

    def set_hazard_enabled(self, enabled: bool) -> None:
        pass

    @property
    def hazard_available(self) -> bool:
        return self._hazard_available


class MemoryRecorder:
    """`SessionRecorder` 와 같은 표면 — 재생의 결정을 파일 대신 목록에 모은다."""

    def __init__(self, clock: Callable[[], int]) -> None:
        self._clock = clock
        self.events: list[dict[str, Any]] = []

    def record(self, kind: str, *, at_ms: int | None = None, **fields: Any) -> None:
        self.events.append({"t": self._clock() if at_ms is None else at_ms, "kind": kind, **fields})

    def record_raw(
        self, kind: str, raw: bytes | str, *, at_ms: int | None = None, **fields: Any
    ) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                self.record(kind, at_ms=at_ms, raw_b64=base64.b64encode(raw).decode(), **fields)
                return
        self.record(kind, at_ms=at_ms, raw=raw, **fields)

    def save_frame(self, _jpeg: bytes, **_fields: Any) -> str:
        return ""


class NullSocket:
    """보낸 것을 모으기만 하는 소켓. 네트워크에 닿지 않는다."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, peer: tuple[str, int]) -> int:
        self.sent.append((data, peer))
        return len(data)

    def settimeout(self, _seconds: float | None) -> None:
        pass

    def close(self) -> None:
        pass


# ── 재생 ─────────────────────────────────────────────────────
def _addr(src: str | None) -> tuple[str, int]:
    if not src or ":" not in src:
        return ("0.0.0.0", 0)
    host, _, port = src.rpartition(":")
    return (host, int(port))


def _raw(event: Mapping[str, Any]) -> bytes:
    if "raw_b64" in event:
        return base64.b64decode(event["raw_b64"])
    return str(event.get("raw", "")).encode("utf-8")


def _apply_operator(runtime: Runtime, event: Mapping[str, Any]) -> bool:
    """관제 입력 하나를 같은 진입점으로 다시 부른다. 모르는 입력이면 거짓."""
    action = event.get("action")
    if action in _PLAIN_ACTIONS:
        getattr(runtime, str(action))()
    elif action == "apply_external":
        name = str(event.get("event"))
        if name not in Event.__members__:  # 이름이 바뀌기 전에 남긴 옛 기록
            return False
        runtime.apply_external(Event[name])
    elif action == "set_mode":
        runtime.set_mode(str(event["target"]))
    elif action == "ask_goto":
        runtime.ask_goto(float(event["x"]), float(event["y"]))
    elif action == "ask_locate_zone":
        runtime.ask_locate_zone(str(event["zone"]))
    elif action == "note_voice_listening":
        runtime.note_voice_listening(event.get("captured_at_ms"))
    elif action == "note_voice_auth":
        runtime.note_voice_auth(bool(event["ok"]), event.get("captured_at_ms"))
    elif action == "send_immediate":
        runtime.send_immediate(str(event["line"]))
    else:
        return False
    return True


def _jsonable(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """기록 파일과 같은 모양(튜플 → 목록 등)으로 맞춘다 — 비교와 결정성 확인이 쉬워진다."""
    return cast("list[dict[str, Any]]", json.loads(json.dumps(events, default=str)))


def replay(
    events: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    device_id: str,
    mode: str | None = None,
    reset_on_start: bool = False,
    robot_ip: str | None = None,
    no_vision: bool | None = None,
    motion_lock: bool = False,
    begin_ms: int | None = None,
) -> list[dict[str, Any]]:
    """사건을 시각 순서대로 `Runtime` 에 넣고 재생이 남긴 사건(기록과 같은 모양)을 돌려준다.

    운용 루프(`Runtime.serve`)와 같은 순서로 깨운다 — 텔레메트리가 오면 받고 한 틱, 아니면
    송신기의 다음 마감에 한 틱. 기록에 영상 판정이 있던 시각도 깨운다(실제 틱 시각이었다).
    """
    ordered = sorted(events, key=lambda e: e["t"])
    if not ordered:
        return []
    config = copy.deepcopy(dict(config))
    vision_spec = config.get("vision")
    if isinstance(vision_spec, dict) and vision_spec.get("collect"):
        vision_spec["collect"] = {}  # 학습용 프레임 수집이 디스크에 쓰지 않게
    start_ms = session_start_ms(ordered, begin_ms)
    shutdown = [e["t"] for e in ordered if e["kind"] == "command_sent" and "repeats" in e]
    end_ms = shutdown[-1] if shutdown else ordered[-1]["t"]

    clock = ReplayClock(start_ms)
    recorder = MemoryRecorder(clock)
    vision_events = [e for e in ordered if e["kind"] == "vision"]
    if no_vision is None:
        no_vision = not vision_events
    vision = None
    if not no_vision:
        results = [(int(e["t"]), vision_from_event(e)[0]) for e in vision_events]
        vision = RecordedVision(
            results,
            clock=clock,
            stall_ms=int(config["vision"]["stall_timeout_ms"]),
            hazard_available=any(e.get("hazard") is not None for e in vision_events),
        )
    runtime = Runtime(
        config,
        device_id=device_id,
        robot_ip=robot_ip,
        clock=clock,
        vision=vision,
        vlm_reader=VlmReader(None),
        mission=Mission(config, mode=mode),
        recorder=cast("Any", recorder),
        motion_lock=motion_lock,
    )
    sock = NullSocket()
    telemetry = deque(e for e in ordered if e["kind"] == "telemetry")
    operators = deque(e for e in ordered if e["kind"] == "operator")
    wakes = deque(sorted({int(e["t"]) for e in vision_events}))

    if reset_on_start:
        runtime.request_reset()  # `main()` 이 기동 전에 부르는 자리 — 입력 기록이 없다
    while operators and operators[0]["t"] <= start_ms:
        _apply_operator(runtime, operators.popleft())
    runtime.begin(cast("Any", sock))
    while runtime.vlm.loading:  # «판독기 없음» 적재는 바로 끝난다 — 첫 틱 전에 기다린다
        time.sleep(0.001)

    now = start_ms
    while True:
        due = runtime.next_due_ms
        wake = now if due is None else max(now, due)
        if telemetry:
            wake = min(wake, max(now, int(telemetry[0]["t"])))
        while wakes and wakes[0] <= now:
            wakes.popleft()
        if wakes:
            wake = min(wake, wakes[0])
        wake = min(max(wake, now), max(end_ms, now))
        while operators and operators[0]["t"] <= wake:
            event = operators.popleft()
            clock.ms = max(now, int(event["t"]))
            _apply_operator(runtime, event)
        now = clock.ms = wake
        if telemetry and telemetry[0]["t"] <= now:
            event = telemetry.popleft()
            runtime.receive(_raw(event), _addr(event.get("src")), now)
        runtime.step(now)
        if now >= end_ms:
            break
    runtime.stop_robot(cast("Any", sock))
    runtime.release()
    return _jsonable(recorder.events)


# ── 비교 ─────────────────────────────────────────────────────
def _commands(events: Sequence[Mapping[str, Any]]) -> list[tuple[int, str]]:
    return [
        (int(e["t"]), normalize_command(line))
        for e in events
        if e["kind"] == "command_sent"
        for line in e.get("lines", [])
    ]


def _transitions(events: Sequence[Mapping[str, Any]]) -> list[tuple[int, str]]:
    return [
        (int(e["t"]), f"{e.get('previous')}->{e.get('state')} ({e.get('trigger')})")
        for e in events
        if e["kind"] == "fsm"
    ]


def _levels(events: Sequence[Mapping[str, Any]]) -> list[tuple[int, str]]:
    return [(int(e["t"]), str(e.get("level"))) for e in events if e["kind"] == "escalation"]


def _align(recorded: list[tuple[int, str]], replayed: list[tuple[int, str]]) -> dict[str, Any]:
    """두 흐름을 순서대로 맞춘다. 일치율 = 맞춘 수 ÷ 긴 쪽 길이."""
    a = [key for _t, key in recorded]
    b = [key for _t, key in replayed]
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    longest = max(len(a), len(b))
    first = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        at = (
            recorded[i1][0]
            if i1 < len(recorded)
            else replayed[j1][0]
            if j1 < len(replayed)
            else None
        )
        first = {
            "t_ms": at,
            "recorded_index": i1,
            "recorded": a[i1:i2][:5],
            "replayed": b[j1:j2][:5],
        }
        break
    return {
        "recorded": len(a),
        "replayed": len(b),
        "matched": matched,
        "match_ratio": 1.0 if longest == 0 else round(matched / longest, 4),
        "first_divergence": first,
    }


def _types(stream: list[tuple[int, str]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for _t, key in stream:
        try:
            counts[str(json.loads(key).get("type", "?"))] += 1
        except (ValueError, AttributeError):
            counts["?"] += 1
    return counts


def compare(
    recorded: Sequence[Mapping[str, Any]], replayed: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """명령·FSM 전이·경보 단계를 기록과 재생 사이에서 맞춰 본다."""
    rec_cmd, rep_cmd = _commands(recorded), _commands(replayed)
    rec_types, rep_types = _types(rec_cmd), _types(rep_cmd)
    report = {
        "commands": _align(rec_cmd, rep_cmd),
        "command_types": {
            name: {"recorded": rec_types.get(name, 0), "replayed": rep_types.get(name, 0)}
            for name in sorted(set(rec_types) | set(rep_types))
        },
        "fsm": _align(_transitions(recorded), _transitions(replayed)),
    }
    # 경보 단계는 runtime_begin 과 같은 때부터 기록했다 — 그 전 형식은 비교하면 가짜 불일치다.
    if any(e["kind"] in ("escalation", "runtime_begin") for e in recorded):
        report["escalation"] = _align(_levels(recorded), _levels(replayed))
    return report


def timeline(events: Sequence[Mapping[str, Any]], start_ms: int, limit: int) -> list[str]:
    """재생의 결정 — 같은 명령이 이어지면 한 줄로 접는다."""
    lines: list[str] = []
    last_key: str | None = None
    repeat = 0

    def flush() -> None:
        if repeat > 1:
            lines[-1] += f"  x{repeat}"

    for event in events:
        kind = event["kind"]
        stamp = f"{(int(event['t']) - start_ms) / 1000:8.3f}s"
        if kind == "command_sent":
            for line in event.get("lines", []):
                key = normalize_command(line)
                if key == last_key:
                    repeat += 1
                    continue
                flush()
                last_key, repeat = key, 1
                lines.append(f"{stamp}  명령  {key}")
        elif kind in ("fsm", "escalation"):
            flush()
            last_key, repeat = None, 0
            if kind == "fsm":
                text = f"상태  {event.get('previous')} -> {event.get('state')} ({event.get('trigger')})"
            else:
                text = f"단계  {event.get('level')}"
            lines.append(f"{stamp}  {text}")
    flush()
    return lines[:limit]


# ── 설정·기록 조건 ────────────────────────────────────────────
def session_options(manifest: Mapping[str, Any]) -> argparse.Namespace | None:
    """기록 당시 `host.runtime` 인자. manifest 에 없으면 `None`."""
    argv = manifest.get("argv")
    if not isinstance(argv, list):
        return None
    try:
        options, _unknown = build_parser().parse_known_args([str(a) for a in argv])
    except SystemExit:
        return None
    return options


def _as_run_config(config: Mapping[str, Any], options: argparse.Namespace | None) -> dict[str, Any]:
    """`main()` 이 로거·기록기 전에 덮어쓰는 것을 똑같이 덮어쓴다 (해시 비교용)."""
    out = dict(config)
    if options is not None and options.log_level:
        out["logging"] = dict(out["logging"], level=options.log_level.upper())
    if options is not None and options.xiao_ip:
        out["network"] = dict(out["network"], xiao_ip=options.xiao_ip)
    return out


def config_sha256(config: Mapping[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_override(config: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """`a.b.c` 자리에 값을 넣은 사본."""
    out = copy.deepcopy(config)
    node = out
    keys = dotted.split(".")
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value
    return out


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def limitations(
    events: Sequence[Mapping[str, Any]], summary: Mapping[str, Any] | None
) -> list[str]:
    """이 세션에서 재생이 재현하지 못하는 것. `summary` 는 기록기의 summary.json(없으면 None)."""
    kinds = Counter(e["kind"] for e in events)
    notes = []
    if not kinds.get("runtime_begin"):
        notes.append(
            "runtime_begin 이 없는 옛 기록 — 기동 시각은 manifest 로 근사했고, "
            "관제 입력(버튼·모드 전환)은 기록되지 않아 재생되지 않는다"
        )
    if any(e["kind"] == "vision" and "sighting" not in e for e in events):
        notes.append("영상 판정 전체가 없는 옛 기록 — 사람 판정을 검출로 근사했다(추적·PPE 없음)")
    if kinds.get("scan") or kinds.get("localization") or kinds.get("navigator_phase"):
        notes.append("LiDAR 길 찾기는 재생하지 않는다 — PATROL 중 MOVE 는 고정 보행 시퀀스가 낸다")
    unknown = Counter(
        str(e.get("action"))
        for e in events
        if e["kind"] == "operator" and e.get("action") not in OPERATOR_ACTIONS
    )
    if unknown:
        names = ", ".join(f"{name} {count}건" for name, count in sorted(unknown.items()))
        notes.append(f"재생기가 모르는 관제 입력({names}) — 재생에서 빠졌다")
    external = Counter(
        str(e.get("event"))
        for e in events
        if e["kind"] == "operator"
        and e.get("action") == "apply_external"
        and str(e.get("event")) not in Event.__members__
    )
    if external:
        names = ", ".join(f"{name} {count}건" for name, count in sorted(external.items()))
        notes.append(f"재생기가 모르는 외부 사건({names}) — 재생에서 빠졌다")
    if summary is None:
        notes.append("summary.json 이 없다 — 기록이 정상 종료되지 않아 버린 사건 수를 모른다")
    elif "read_error" in summary:
        notes.append(
            f"summary.json 을 읽지 못했다({summary['read_error']}) — 버린 사건 수를 모른다"
        )
    else:
        if summary.get("dropped"):
            notes.append(
                f"기록 큐가 넘쳐 사건 {summary['dropped']}건을 버렸다 — "
                "빠진 입력 때문에 재생이 기록과 갈릴 수 있다"
            )
        if summary.get("failed"):
            notes.append(f"기록 쓰기 실패({summary['failed']}) — 그 뒤 사건이 빠졌을 수 있다")
    return notes


# ── CLI ──────────────────────────────────────────────────────
def _parse_set(items: Sequence[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set 은 key.path=value 모양이어야 한다: {item!r}")
        overrides[key] = yaml.safe_load(raw)
    return overrides


def _print_report(name: str, report: Mapping[str, Any], start_ms: int) -> None:
    cmd = report["commands"]
    print(
        f"[{name}] 명령 일치 {cmd['matched']}/{max(cmd['recorded'], cmd['replayed'])} "
        f"({cmd['match_ratio'] * 100:.1f}%) — 기록 {cmd['recorded']} · 재생 {cmd['replayed']}"
    )
    fsm = report["fsm"]
    print(f"[{name}] FSM 전이 일치 {fsm['matched']}/{max(fsm['recorded'], fsm['replayed'])}")
    if "escalation" in report:
        esc = report["escalation"]
        print(f"[{name}] 경보 단계 일치 {esc['matched']}/{max(esc['recorded'], esc['replayed'])}")
    for label, part in (("명령", cmd), ("FSM", fsm)):
        first = part["first_divergence"]
        if first is not None:
            at = "?" if first["t_ms"] is None else f"{(first['t_ms'] - start_ms) / 1000:.3f}s"
            print(f"[{name}] 첫 {label} 불일치 {at} (기동 기준)")
            print(f"    기록: {first['recorded']}")
            print(f"    재생: {first['replayed']}")


def main(argv: list[str] | None = None) -> int:
    survive_encoding_errors()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("session", type=Path, help="--record-dir 로 남긴 기록 폴더")
    parser.add_argument("--device", default=None, help="개체 id (기본: manifest 의 --device)")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="«이 설정이었다면» — 설정 한 값을 바꿔 한 번 더 재생한다 (YAML 값, 여러 번 가능)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="개체 설정 대신 이 설정 YAML 전체로 재생한다 (기본: load_config(--device))",
    )
    parser.add_argument(
        "--overlay", type=Path, default=None, help="덮어쓸 설정 YAML (--set 과 같이)"
    )
    parser.add_argument("--json", type=Path, default=None, help="보고서를 JSON 으로 쓴다")
    parser.add_argument("--timeline", type=int, default=40, help="출력할 재생 결정 줄 수")
    parser.add_argument("--verbose", action="store_true", help="런타임 로그를 보인다")
    args = parser.parse_args(argv)
    # 런타임 로그는 기본으로 끈다 — 재생 보고가 묻힌다. 끝나면 되돌린다(같은 프로세스의 시험).
    runtime_log = logging.getLogger("mechadog")
    previous_level = runtime_log.level
    if not args.verbose:
        runtime_log.setLevel(logging.CRITICAL)
    try:
        return _run(args, parser)
    finally:
        runtime_log.setLevel(previous_level)


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    manifest, events = load_session(args.session)
    options = session_options(manifest)
    device = args.device or (options.device if options is not None else None)
    if not device:
        parser.error("manifest 에 --device 가 없다 — --device 로 지정한다")
    config = (
        load_config(device)
        if args.config is None
        else yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    recorded_hash = manifest.get("config_sha256")
    replay_kwargs: dict[str, Any] = {
        "device_id": device,
        "mode": None if options is None else options.mode,
        "reset_on_start": bool(options is not None and options.reset_on_start),
        "robot_ip": None if options is None else options.robot_ip,
        "no_vision": None if options is None or not options.no_vision else True,
        "motion_lock": bool(options is not None and options.motion_lock),
        "begin_ms": manifest_begin_ms(manifest),
    }
    start_ms = session_start_ms(events, replay_kwargs["begin_ms"])

    baseline = replay(events, config, **replay_kwargs)
    report: dict[str, Any] = {
        "session": str(args.session),
        "device": device,
        "events": len(events),
        "duration_s": round(((events[-1]["t"] - events[0]["t"]) / 1000.0) if events else 0.0, 2),
        "config_matches_recording": (
            None
            if recorded_hash is None
            else config_sha256(_as_run_config(config, options)) == recorded_hash
        ),
        "limitations": limitations(events, load_summary(args.session)),
        "baseline": compare(events, baseline),
        "timeline": timeline(baseline, start_ms, args.timeline),
    }
    overrides = _parse_set(args.set)
    if overrides or args.overlay is not None:
        changed = dict(config)
        if args.overlay is not None:
            changed = _merge(
                changed, yaml.safe_load(args.overlay.read_text(encoding="utf-8")) or {}
            )
        for key, value in overrides.items():
            changed = apply_override(changed, key, value)
        what_if = replay(events, changed, **replay_kwargs)
        report["overrides"] = overrides
        report["overlay"] = None if args.overlay is None else str(args.overlay)
        report["what_if"] = compare(events, what_if)
        report["what_if_vs_baseline"] = compare(baseline, what_if)
        report["what_if_timeline"] = timeline(what_if, start_ms, args.timeline)

    print(f"세션 {args.session} · 개체 {device} · 사건 {len(events)} · {report['duration_s']}s")
    if report["config_matches_recording"] is False:
        print("주의: 지금 설정의 해시가 기록 당시와 다르다 — 불일치의 원인일 수 있다")
    for note in report["limitations"]:
        print(f"- 재현 한계: {note}")
    _print_report("기록 대비", report["baseline"], start_ms)
    if "what_if" in report:
        _print_report("바꾼 설정 대비 기록", report["what_if"], start_ms)
        _print_report("바꾼 설정 대비 기준 재생", report["what_if_vs_baseline"], start_ms)
    print("재생 결정:")
    for line in report["timeline"]:
        print("  " + line)
    if args.json is not None:
        args.json.write_text(
            json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
