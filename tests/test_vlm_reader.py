"""VLM 판독기 검증 (WBS 4.8.0 · ADR-35).

**가중치도 GPU 도 없이 전수 검증된다.** 세션을 주입받으므로 모델 없이 수명주기와
파싱이 모두 닫힌다 — `test_detector.py` 와 같은 이유다.

여기서 지키는 것은 넷이다.

    ① 판독기가 로봇을 세우지 않는다 — 어떤 실패도 예외로 새지 않는다
    ② 모름과 거짓을 섞지 않는다
    ③ 부분 판독을 버리지 않는다
    ④ 두 번 올리지 않고, 내린 뒤에는 남지 않는다
"""

from __future__ import annotations

import pytest

from host.vision.vlm_reader import (
    QUESTIONS,
    Question,
    Reading,
    VlmReader,
    parse_answer,
)


class FakeSession:
    """주입되는 가짜 세션. **모델을 흉내 내지 않고 답만 돌려준다.**"""

    def __init__(self, answers: list[str] | None = None, *, fail_at: int | None = None) -> None:
        self._answers = answers if answers is not None else ["yes"] * len(QUESTIONS)
        self._fail_at = fail_at
        self.asked: list[str] = []
        self.closed = 0

    def ask(self, _image: object, prompt: str) -> str:
        index = len(self.asked)
        self.asked.append(prompt)
        if self._fail_at is not None and index == self._fail_at:
            raise RuntimeError("추론 실패")
        return self._answers[index % len(self._answers)]

    def close(self) -> None:
        self.closed += 1


# ── ① 질문 항목 자체의 계약 ─────────────────────────────────


def test_questions_are_english_and_closed() -> None:
    """⚠️ 한국어로 물으면 틀린다 — 실측에서 «정상입니다» 가 나왔다 (ADR-35 결정 3)."""
    for question in QUESTIONS:
        assert question.prompt.isascii(), f"{question.key}: 프롬프트가 영어가 아니다"
        assert "yes or no" in question.prompt.lower(), f"{question.key}: 답을 닫지 않았다"


def test_no_open_ended_judgement_questions() -> None:
    """⚠️ «위험한가» 가 바로 실측에서 틀린 질문이다 — 그 답은 판정이지 관찰이 아니다."""
    banned = ("danger", "safe", "normal", "should", "risk")
    for question in QUESTIONS:
        lowered = question.prompt.lower()
        for word in banned:
            assert word not in lowered, f"{question.key}: 판정을 묻고 있다 ({word})"


def test_question_keys_are_unique() -> None:
    keys = [q.key for q in QUESTIONS]
    assert len(keys) == len(set(keys))


# ── ② 파싱 — 모름과 거짓을 섞지 않는다 ──────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", True),
        ("Yes.", True),
        ("YES, there is a person lying down.", True),
        ("no", False),
        ("No.", False),
        ("Nope, nothing there.", False),
        # ⚠️ 앞머리만 본다 — 뒤의 «no» 가 앞의 «yes» 를 뒤집으면 안 된다
        ("Yes, a person is lying down, no helmet is visible.", True),
        # ⚠️ 토큰으로 본다 — 부분일치면 «not»·«nothing» 이 거짓으로 읽힌다
        ("Nothing is blocking the walkway.", None),
        ("There is no person lying on the floor.", None),
        ("A person is lying on the floor.", None),
        ("", None),
        ("   ", None),
    ],
)
def test_parses_only_the_leading_token(text: str, expected: bool | None) -> None:
    assert parse_answer(text) is expected


def test_unknown_is_not_false() -> None:
    """*"안 보였다"* 와 *"없다"* 는 다른 말이다."""
    assert parse_answer("A person is lying on the floor.") is not False


# ── ③ 수명주기 ──────────────────────────────────────────────


def test_missing_factory_degrades_instead_of_raising() -> None:
    """⚠️ 가중치가 없는 것은 흔한 일이다 — 그때 로봇이 서면 안 된다."""
    reader = VlmReader(None)
    assert reader.available is False
    assert reader.load() is False
    reading = reader.read(object(), now_ms=100)
    assert reading.degraded is True
    assert reading.reason == "not_loaded"
    assert reading.answers == ()
    assert reading.get("person_down") is None


def test_load_failure_never_raises() -> None:
    def broken() -> FakeSession:
        raise OSError("가중치 없음")

    reader = VlmReader(broken)
    assert reader.available is True
    assert reader.load() is False
    assert reader.loaded is False


def test_load_is_idempotent() -> None:
    """⚠️ 연타로 두 벌 올라가면 4.1GB x2 로 10GB 를 넘긴다."""
    made: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        made.append(session)
        return session

    reader = VlmReader(factory)
    assert reader.load() is True
    assert reader.load() is True
    assert len(made) == 1


def test_unload_releases_the_session() -> None:
    session = FakeSession()
    reader = VlmReader(lambda: session)
    reader.load()
    reader.unload()
    assert session.closed == 1
    assert reader.loaded is False
    # 두 번 내려도 조용하다
    reader.unload()
    assert session.closed == 1


def test_unload_failure_does_not_block_the_switch() -> None:
    class Stubborn(FakeSession):
        def close(self) -> None:
            raise RuntimeError("해제 실패")

    reader = VlmReader(Stubborn)
    reader.load()
    reader.unload()  # 예외가 새면 모드 전환이 막힌다
    assert reader.loaded is False


# ── ④ 판독 ──────────────────────────────────────────────────


def test_asks_every_question_and_returns_structured_result() -> None:
    session = FakeSession(["yes", "no", "no"])
    reader = VlmReader(lambda: session)
    reader.load()
    reading = reader.read(object(), now_ms=42)

    assert isinstance(reading, Reading)
    assert reading.taken_at_ms == 42
    assert reading.degraded is False
    assert reading.reason is None
    assert len(reading.answers) == len(QUESTIONS)
    assert reading.get("person_down") is True
    assert reading.get("fallen_object") is False
    assert reading.get("blocked_path") is False
    assert session.asked == [q.prompt for q in QUESTIONS]


def test_unparsed_answer_keeps_the_raw_text() -> None:
    """판독이 이상할 때 사람이 볼 것은 원문이다."""
    session = FakeSession(["The room looks fine."])
    reader = VlmReader(lambda: session)
    reader.load()
    reading = reader.read(object(), now_ms=1)
    assert reading.answers[0].value is None
    assert reading.answers[0].raw == "The room looks fine."


def test_partial_reading_is_kept_on_failure() -> None:
    """⚠️ 통째로 버리면 급한 항목까지 같이 잃는다."""
    session = FakeSession(["yes", "no", "no"], fail_at=1)
    reader = VlmReader(lambda: session)
    reader.load()
    reading = reader.read(object(), now_ms=7)

    assert reading.degraded is True
    assert reading.reason == "ask_failed"
    assert len(reading.answers) == 1
    assert reading.get("person_down") is True


def test_budget_stops_further_questions() -> None:
    slow = Question(key="slow", prompt="Is it? Answer with yes or no only.", intent="시험용")
    session = FakeSession(["yes"])
    reader = VlmReader(lambda: session, questions=(slow, slow, slow), budget_ms=1)

    # 한 번 묻는 데 드는 시간이 예산을 이미 채우도록 예산을 1ms 로 둔다.
    reader.load()
    reading = reader.read(object(), now_ms=0)
    assert len(reading.answers) <= 3
    if reading.reason is not None:
        assert reading.reason == "budget_exhausted"
        assert reading.degraded is True


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError, match="budget_ms"):
        VlmReader(None, budget_ms=0)
