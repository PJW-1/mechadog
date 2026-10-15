"""LiDAR 막힘 확정 프레임의 VLM 원인 판독 — `PathCause` 단독 검증 (ADR-45).

LiDAR 가 막힘을 확정한 프레임에 «무너진 물건인가» 하나만 묻고, 답(또는 대기 상한 초과)이 오면
`path_blocked` 를 **한 번만** 남긴다. 틱을 거치는 실제 스레드 판독은 `test_runtime_lidar.py` 에 있다.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from host.behavior.mission import Mission
from host.behavior.path_cause import PATH_CAUSE_KEY, PathCause
from host.vision.vlm_reader import QUESTIONS, Answer, Reading

T0 = 1_000_000
#: 공장 모드의 선행 기능 검사를 연다 — 원인 판독은 공장 모드에서만 묻는다.
pytestmark = pytest.mark.usefixtures("unlock_modes")

FRAME = SimpleNamespace(jpeg=b"jpeg", frame_received_ms=T0)


def _frame_at(received_ms: int) -> SimpleNamespace:
    """그 시각에 받은 프레임 — 막힘 확정 때의 장면이다."""
    return SimpleNamespace(jpeg=b"jpeg", frame_received_ms=received_ms)


HIT = {"x": 2.5, "y": 2.0, "target": "B", "source": "lidar"}


def _reading(raw: str | None, *, latency_ms: int = 420) -> Reading:
    """`blocked_by_fallen` 하나의 판독. `raw` 가 `None` 이면 저하된(빈) 판독이다."""
    if raw is None:
        return Reading(answers=(), degraded=True, reason="ask_failed", taken_at_ms=T0)
    value = {"yes": True, "no": False}.get(raw.lower())
    answer = Answer(PATH_CAUSE_KEY, value, raw, latency_ms)
    return Reading(answers=(answer,), degraded=False, reason=None, taken_at_ms=T0)


class OneSlotVlm:
    """`VlmWorker` 대역 — `busy` 가 거짓이 되면 `slot` 에 넣어 둔 판독이 나온다."""

    def __init__(self) -> None:
        self.available = True
        self.busy = False
        self.accept = True
        self.slot: Reading | None = None
        self.submitted: list[tuple[int, tuple[str, ...]]] = []

    def submit(self, _image, *, now_ms: int, keys=None) -> bool:
        if not self.available or self.busy or not self.accept:
            return False
        self.submitted.append((now_ms, tuple(keys)))
        return True

    def take(self) -> Reading | None:
        reading, self.slot = self.slot, None
        return reading


@pytest.fixture
def conf(cfg: dict) -> dict:
    """세션 설정의 사본 — 스위치·상한을 고쳐도 다른 시험에 새지 않는다."""
    return copy.deepcopy(cfg)


@pytest.fixture
def parts(conf: dict):
    vlm = OneSlotVlm()
    others = SimpleNamespace(waiting=False)
    records: list[tuple[str, object, dict]] = []
    said: list[tuple[str, dict]] = []
    cause = PathCause(
        conf,
        mission=Mission(conf, mode="factory"),
        vlm=vlm,
        record=lambda kind, frame, judgement: records.append((kind, frame, judgement)),
        announce=lambda kind, judgement: said.append((kind, judgement)),
        others_waiting=lambda: others.waiting,
    )
    return SimpleNamespace(cause=cause, vlm=vlm, others=others, records=records, said=said)


def _block(parts, frame=FRAME, now_ms: int = T0) -> None:
    parts.cause.blocked(dict(HIT), frame, now_ms)


# ── 질문 ───────────────────────────────────────────────────────


def test_the_question_is_fixed_and_closed() -> None:
    (question,) = [q for q in QUESTIONS if q.key == PATH_CAUSE_KEY]
    assert question.prompt == (
        "Is the walkway blocked by an object that has fallen over or collapsed? "
        "Answer with yes or no only."
    )
    assert "ADR-45" in question.intent


def test_the_zone_visit_does_not_ask_it() -> None:
    from host.behavior.zone_inspector import HAZARD_ITEM, ZONE_KEYS

    assert PATH_CAUSE_KEY not in (*ZONE_KEYS, HAZARD_ITEM)


# ── 답 → `fallen` ──────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "fallen"), [("Yes", True), ("No", False), ("maybe", None)])
def test_the_answer_lands_in_the_judgement_once(parts, raw: str, fallen: bool | None) -> None:
    _block(parts)
    assert parts.vlm.submitted == [(T0, (PATH_CAUSE_KEY,))], "확정 프레임에 그 질문 하나만 건다"
    assert parts.cause.waiting
    assert parts.records == [], "답을 받기 전에는 기록하지 않는다"
    parts.vlm.busy = True
    parts.cause.poll(T0 + 100)
    assert parts.records == []
    parts.vlm.busy = False
    parts.vlm.slot = _reading(raw)
    parts.cause.poll(T0 + 600)
    parts.cause.poll(T0 + 5000)
    assert not parts.cause.waiting
    (record,) = parts.records
    kind, frame, judgement = record
    assert kind == "path_blocked"
    assert frame is FRAME, "LiDAR 가 막힘을 확정한 그 프레임으로 남긴다"
    assert judgement == {
        **HIT,
        "fallen": fallen,
        "vlm_path_cause": False,
        "vlm_reason": None,
        "raw": raw,
        "latency_ms": 420,
        "wait_ms": 600,
    }


def test_a_degraded_reading_is_unknown(parts) -> None:
    _block(parts)
    parts.vlm.slot = _reading(None)
    parts.cause.poll(T0 + 300)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "ask_failed"
    assert judgement["raw"] is None


def test_a_dead_worker_records_unknown(parts) -> None:
    """워커가 죽어 `take()` 가 `None` 이어도 기록은 한 번 나간다."""
    _block(parts)
    parts.cause.poll(T0 + 300)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "worker_failed"
    assert not parts.cause.waiting


# ── 대기 상한 ───────────────────────────────────────────────────


def test_the_wait_is_bounded_and_the_late_answer_is_only_logged(
    parts, conf: dict, caplog: pytest.LogCaptureFixture
) -> None:
    wait_ms = int(conf["vision"]["vlm"]["path_cause_wait_ms"])
    _block(parts)
    parts.vlm.busy = True
    parts.cause.poll(T0 + wait_ms - 1)
    assert parts.records == []
    parts.cause.poll(T0 + wait_ms)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "timeout"
    assert judgement["wait_ms"] == wait_ms
    # 늦은 판독은 슬롯을 비울 때까지 쥐고 있다 — 다른 판독이 걸면 결과가 섞인다.
    assert parts.cause.waiting
    parts.vlm.busy = False
    parts.vlm.slot = _reading("yes")
    with caplog.at_level("INFO"):
        parts.cause.poll(T0 + wait_ms + 300)
    assert not parts.cause.waiting
    assert parts.vlm.slot is None, "늦은 판독을 주워 슬롯을 비운다"
    assert len(parts.records) == 1, "늦은 답으로 다시 기록하지 않는다"
    assert "path_cause_late" in caplog.text


def test_a_new_block_does_not_ask_until_the_late_answer_is_cleared(parts, conf: dict) -> None:
    """상한을 넘긴 앞 판독이 끝나 슬롯에 남아 있는 동안 새 막힘이 걸면, 새 답이 «늦은 판독» 으로
    버려진다. 늦은 결과를 먼저 비운 뒤에 건다."""
    wait_ms = int(conf["vision"]["vlm"]["path_cause_wait_ms"])
    _block(parts)
    parts.vlm.busy = True
    parts.cause.poll(T0 + wait_ms)  # A 는 timeout 으로 남는다
    assert parts.cause.waiting
    # A 의 스레드가 끝났다 — 결과는 아직 슬롯에 있다(실제 `VlmWorker` 는 끝난 스레드면 받는다).
    parts.vlm.busy = False
    parts.vlm.slot = _reading("no")
    later = T0 + wait_ms + 100
    # `ScanRelay.observe` 가 `poll` 보다 먼저 돈다.
    parts.cause.blocked({**HIT, "target": "C"}, _frame_at(later), later)
    assert len(parts.vlm.submitted) == 1, "늦은 결과가 슬롯에 있는 동안 걸지 않는다"
    parts.cause.poll(later)  # 늦은 결과를 비우고 같은 호출에서 B 를 건다
    assert parts.vlm.slot is None
    assert parts.vlm.submitted[-1] == (later, (PATH_CAUSE_KEY,))
    parts.vlm.slot = _reading("yes")
    parts.cause.poll(later + 400)
    a, b = (judgement for _kind, _frame, judgement in parts.records)
    assert (a["fallen"], a["vlm_reason"]) == (None, "timeout")
    assert b["target"] == "C"
    assert (b["fallen"], b["vlm_reason"]) == (True, None)


def test_a_block_that_never_asked_keeps_the_live_drain(parts, conf: dict) -> None:
    """못 건 막힘이 상한을 넘겨도 앞 막힘의 살아 있는 배수를 지우지 않는다.

    지우면 앞 판독의 결과가 슬롯에 남고, 다음 막힘의 판독이 슬롯을 못 쓰고 끝났을 때
    그 낡은 답을 제 것으로 기록한다.
    """
    wait_ms = int(conf["vision"]["vlm"]["path_cause_wait_ms"])
    _block(parts)  # A 를 건다
    parts.vlm.busy = True
    parts.cause.poll(T0 + wait_ms)  # A timeout — A 스레드는 아직 돈다
    t_b = T0 + wait_ms + 100
    parts.cause.blocked({**HIT, "target": "B"}, _frame_at(t_b), t_b)  # 배수 중이라 못 건다
    parts.cause.poll(t_b + wait_ms)  # B 도 상한 초과(busy)
    assert parts.cause.waiting, "A 의 판독이 아직 돌고 있다 — 배수를 지우지 않는다"
    parts.vlm.busy = False
    parts.vlm.slot = _reading("yes")  # A 의 낡은 답이 슬롯에 남는다
    t_c = t_b + wait_ms + 100
    parts.cause.blocked({**HIT, "target": "C"}, _frame_at(t_c), t_c)
    parts.cause.poll(t_c)  # 낡은 답을 비우고 C 를 건다
    assert parts.vlm.slot is None
    assert parts.vlm.submitted[-1] == (t_c, (PATH_CAUSE_KEY,))
    # C 의 판독이 슬롯을 못 쓰고 끝났다(워커 예외) — 슬롯은 비어 있다.
    parts.cause.poll(t_c + 300)
    c = parts.records[-1][2]
    assert c["target"] == "C"
    assert (c["fallen"], c["vlm_reason"]) == (None, "worker_failed"), (
        "A 의 낡은 답을 C 에 싣지 않는다"
    )


def test_a_stale_frame_is_not_asked_and_not_photographed(parts, conf: dict) -> None:
    """비전이 끊겨 받은 지 오래된 프레임은 확정 시점의 장면이 아니다 — 묻지 않고 사진도 남기지 않는다."""
    max_age = int(conf["vision"]["vlm"]["path_cause_max_frame_age_ms"])
    _block(parts, frame=_frame_at(T0 - max_age))  # 상한 그대로는 아직 쓴다
    assert len(parts.vlm.submitted) == 1
    parts.vlm.slot = _reading("no")
    parts.cause.poll(T0 + 100)
    late = T0 + 10_000
    parts.cause.blocked(dict(HIT), _frame_at(late - max_age - 1), late)
    assert len(parts.vlm.submitted) == 1, "낡은 프레임으로 묻지 않는다"
    assert len(parts.records) == 1, "낡은 사진으로 블랙박스를 남기지 않는다"
    (kind, judgement) = parts.said[-1]
    assert kind == "path_blocked"
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "stale_frame"
    assert not parts.cause.waiting


# ── 워커를 나눠 쓴다 ────────────────────────────────────────────


def test_it_waits_for_the_other_reading_within_the_bound(parts) -> None:
    """쓰러짐·구역 판독이 걸려 있으면 걸지 않고, 상한 안에서 비면 그때 같은 프레임을 건다."""
    parts.others.waiting = True
    _block(parts)
    assert parts.vlm.submitted == []
    assert parts.cause.waiting, "걸려고 기다리는 동안 다른 판독이 새로 걸지 않게 한다"
    parts.cause.poll(T0 + 500)
    assert parts.vlm.submitted == []
    parts.others.waiting = False
    parts.cause.poll(T0 + 700)
    assert parts.vlm.submitted == [(T0 + 700, (PATH_CAUSE_KEY,))]
    parts.vlm.slot = _reading("yes")
    parts.cause.poll(T0 + 1000)
    (_kind, frame, judgement) = parts.records[0]
    assert frame is FRAME
    assert judgement["fallen"] is True
    assert judgement["wait_ms"] == 1000


def test_a_worker_that_never_frees_up_gives_unknown(parts, conf: dict) -> None:
    wait_ms = int(conf["vision"]["vlm"]["path_cause_wait_ms"])
    parts.vlm.busy = True
    _block(parts)
    parts.cause.poll(T0 + wait_ms - 1)
    assert parts.records == []
    parts.cause.poll(T0 + wait_ms)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "busy"
    assert not parts.cause.waiting, "건 적이 없으니 쥘 슬롯도 없다"
    assert parts.vlm.submitted == []


def test_a_second_block_while_waiting_is_recorded_at_once(parts) -> None:
    _block(parts)
    parts.cause.blocked({**HIT, "x": 3.0}, FRAME, T0 + 200)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["x"] == 3.0
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "busy"
    assert len(parts.vlm.submitted) == 1


# ── 바로 기록하는 경우 ──────────────────────────────────────────


def test_no_vlm_records_at_once(parts) -> None:
    parts.vlm.available = False
    _block(parts)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "not_loaded"
    assert judgement["wait_ms"] == 0
    assert not parts.cause.waiting


def test_no_frame_announces_at_once(parts) -> None:
    _block(parts, frame=None)
    assert parts.records == []
    (kind, judgement) = parts.said[0]
    assert kind == "path_blocked"
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "no_frame"
    assert parts.vlm.submitted == []


def test_guard_mode_does_not_ask(parts, conf: dict) -> None:
    """경비 모드는 VLM 판독을 돌리지 않는다 (`mission.enables("change_detect")`)."""
    cause = PathCause(
        conf,
        mission=Mission(conf, mode="guard"),
        vlm=parts.vlm,
        record=lambda *args: parts.records.append(args),
        announce=lambda *args: parts.said.append(args),
        others_waiting=lambda: False,
    )
    cause.blocked(dict(HIT), FRAME, T0)
    assert parts.vlm.submitted == []
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["fallen"] is None
    assert judgement["vlm_reason"] == "mission"


# ── 스위치·설정 ─────────────────────────────────────────────────


def test_the_switch_is_carried_into_the_judgement(parts, conf: dict) -> None:
    conf["change_detect"]["vlm_path_cause"] = True
    cause = PathCause(
        conf,
        mission=Mission(conf, mode="factory"),
        vlm=parts.vlm,
        record=lambda *args: parts.records.append(args),
        announce=lambda *args: parts.said.append(args),
        others_waiting=lambda: False,
    )
    cause.blocked(dict(HIT), FRAME, T0)
    parts.vlm.slot = _reading("yes")
    cause.poll(T0 + 300)
    (_kind, _frame, judgement) = parts.records[0]
    assert judgement["vlm_path_cause"] is True
    assert judgement["fallen"] is True


def test_the_defaults_are_off_and_bounded(conf: dict) -> None:
    assert conf["change_detect"]["vlm_path_cause"] is False
    assert conf["vision"]["vlm"]["path_cause_wait_ms"] == 1500
    assert conf["vision"]["vlm"]["path_cause_max_frame_age_ms"] == 1000


@pytest.mark.parametrize("key", ["path_cause_wait_ms", "path_cause_max_frame_age_ms"])
@pytest.mark.parametrize("wait_ms", [0, -1])
def test_a_non_positive_wait_is_refused(conf: dict, key: str, wait_ms: int) -> None:
    conf["vision"]["vlm"][key] = wait_ms
    with pytest.raises(ValueError, match=key):
        PathCause(
            conf,
            mission=Mission(conf, mode="factory"),
            vlm=OneSlotVlm(),
            record=lambda *_: None,
            announce=lambda *_: None,
            others_waiting=lambda: False,
        )
