"""로컬 VLM 상황 판독 (WBS 4.8.0 · FR-8 · ADR-35).

**객체 목록으로는 «쓰러져 있다» 를 말할 수 없다.** COCO 80 에 소화기·사다리·공구함이
없으므로 넘어졌는지 이전에 물건 자체가 보이지 않는다. 그래서 사진을 그대로 읽는
모델을 하나 두되, **판단은 주지 않는다.**

    ① QUESTIONS      고정 **영어** 질문 항목 — 데이터일 뿐이다
    ② parse_answer   모델이 뱉은 글 → 판독 하나. **모델도 GPU 도 없이 전수 검증된다**
    ③ VlmReader      적재·해제·질의. 세션은 주입받는다

`detector.py` 의 세 겹과 같은 모양이다. 다른 점은 아래 둘이고, 둘 다 의도한 것이다.

⚠️ **가중치가 없어도 멈추지 않는다.** 검출기는 `ModelMissingError` 로 즉시 서지만
이쪽은 Tier 3 이라 **기능 저하로 기록하고 지나간다**(ADR-35 결정 6). 사람 인지와
주행은 VLM 없이 그대로 돌아야 한다 — 판독기가 로봇을 세우면 Tier 구분이 무의미해진다.

⚠️ **판정하지 않는다.** 이 모듈은 사건 이름을 만들지 않고 관찰만 돌려준다. 실측에서
열린 질문(*"위험한가?"*)에는 쓰러진 작업자를 앞에 두고도 `NORMAL` 이라 답했다
(ADR-35 결정 2). 항목을 쪼개 닫힌 질문으로 묻고, 사건은 FSM 이 만든다.

⚠️ **영어로 묻는다.** 같은 사진에 한국어로 물으면 *"정상입니다"*, 영어로 물으면
*"person lying on the floor"* 가 나왔다. 2B 모델의 한계이며 한국어 문장은 이미 도는
LLM 이 쓴다 (`4.8.1`).

⚠️ **메인 루프에서 적재하지 마라.** 적재가 14.5초다. 로봇은 `safety.cmd_timeout_ms`
(300ms) 동안 명령을 못 받으면 스스로 선다. `worker.py` 가 추론을 스레드로 뺀 것과
같은 이유로, `load()` 는 메인 루프 밖에서 불러야 한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")


@dataclass(frozen=True, slots=True)
class Question:
    """물어볼 항목 하나.

    ⚠️ **`prompt` 는 영어이고 «yes or no» 로 닫아 둔다.** 열어 두면 모델이 서술로
    답하고, 서술은 파싱이 추측이 된다. 닫힌 질문은 실측에서 정확히 맞혔다.
    """

    #: 결과를 찾아 쓰는 키. 로그와 사건 쪽에서 쓰는 이름이다.
    key: str
    #: 모델에게 그대로 가는 문장. **번역하지 마라** (위 경고).
    prompt: str
    #: 이 항목이 무엇을 보려는 것인지. 사람이 읽는 자리다.
    intent: str


#: 고정 질문 셋 (ADR-35 결정 2). **늘릴 때는 질문당 0.2~0.9초가 붙는다.**
#:
#: ⚠️ **«위험한가» 류를 넣지 마라.** 그것이 실측에서 틀린 바로 그 질문이며, 그 답을
#: 받는 순간 판정이 모델로 넘어간다. 여기 있는 셋은 모두 **본 것**을 묻는다.
QUESTIONS: tuple[Question, ...] = (
    Question(
        key="person_down",
        prompt="Is there a person lying on the floor? Answer with yes or no only.",
        intent="쓰러진 사람 (FR-9) — 가장 급한 사건",
    ),
    Question(
        key="fallen_object",
        prompt="Is there an object that has fallen over or collapsed? Answer with yes or no only.",
        intent="무너진 물건 (FR-8.3) — COCO 어휘 밖이라 검출기가 못 잡는다",
    ),
    Question(
        key="blocked_path",
        prompt="Is the walkway blocked by an obstacle? Answer with yes or no only.",
        intent="막힌 통로 — 사람이 없는 구역에서도 봐야 한다",
    ),
)

#: 답 앞머리에서 찾는 토큰. **뒤쪽은 보지 않는다** — `parse_answer` 주석 참조.
_YES = frozenset({"yes", "yeah", "yep"})
_NO = frozenset({"no", "nope", "none"})


@dataclass(frozen=True, slots=True)
class Answer:
    """항목 하나의 판독.

    ⚠️ **`value` 는 3값이다 — 참·거짓·모름(`None`).** 모름을 거짓으로 접으면
    *"안 보였다"* 와 *"없다"* 가 같은 말이 되는데, 그 둘은 전혀 다르다.
    """

    key: str
    #: 참/거짓, 또는 읽어 내지 못했으면 `None`
    value: bool | None
    #: 모델이 실제로 뱉은 글. **판독이 이상할 때 이것부터 본다**
    raw: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class Reading:
    """한 프레임의 판독 묶음.

    ⚠️ **부분 실패가 정상이다.** 예산을 넘겨 뒤쪽 항목을 못 물었으면 그만큼만 담고
    `degraded` 를 세운다 — 통째로 버리면 급한 항목(`person_down`)까지 같이 잃는다.
    """

    answers: tuple[Answer, ...]
    #: 기능 저하 여부. 미적재·타임아웃·예외가 모두 여기로 모인다 (ADR-35 결정 6)
    degraded: bool
    #: 저하 사유. 정상이면 `None`
    reason: str | None
    taken_at_ms: int

    def get(self, key: str) -> bool | None:
        """항목 하나의 값. 못 물어봤거나 못 읽었으면 `None`."""
        for answer in self.answers:
            if answer.key == key:
                return answer.value
        return None


class VlmSession(Protocol):
    """적재된 모델 한 벌. **이 모듈은 이것을 만들지 않는다.**

    파일도 GPU 도 만지지 않는 이유는 `detector.py` 와 같다 — 가중치 없이 전수
    검증하기 위해서다. 실제 적재는 주입되는 쪽에 둔다.
    """

    def ask(self, image: Any, prompt: str) -> str:
        """이미지 한 장에 질문 하나. 답을 글로 돌려준다."""
        ...

    def close(self) -> None:
        """VRAM 을 놓는다. **종료할 때 이것이 안 불리면 프로세스가 끝날 때까지 VRAM 에 남는다.**"""
        ...


def parse_answer(text: str) -> bool | None:
    """모델의 글에서 참/거짓을 읽는다. 못 읽으면 `None`.

    ⚠️ **앞머리만 본다.** 글 전체에서 부정어를 찾으면
    *"Yes, a person is lying down, no helmet is visible"* 이 거짓으로 뒤집힌다.
    닫힌 질문을 던졌으므로 답은 앞에 온다.

    ⚠️ **부분일치가 아니라 토큰으로 본다.** `no` 는 `not`·`nothing`·`nobody` 의
    앞부분이기도 해서, 앞에서부터 글자만 모아 **한 단어를 만든 뒤** 견준다.
    """
    head = text.strip().lower()
    if not head:
        return None
    token = ""
    for char in head:
        if char.isalpha():
            token += char
        else:
            break
    if token in _YES:
        return True
    if token in _NO:
        return False
    return None


class VlmReader:
    """판독기 수명주기. 기동할 때 한 번 올리고 종료할 때 내린다 (ADR-35 결정 5 ·
    2026-09-24 개정: 상시 적재).

    ⚠️ **두 번 적재하지 않는다.** 4.1GB 가 두 벌 올라가면 10GB 를 넘긴다. `load()` 는
    멱등이다 — **단, 스레드 안전하지는 않다.** 적재 중에 또 부르면 `_session` 이 아직
    비어 있어 두 벌을 올린다. 그래서 `VlmWorker.start()` 가 기동 때 한 번만 부른다.
    """

    def __init__(
        self,
        session_factory: Callable[[], VlmSession] | None,
        *,
        questions: Sequence[Question] = QUESTIONS,
        budget_ms: int = 3000,
    ) -> None:
        if budget_ms <= 0:
            raise ValueError(f"budget_ms 는 0 보다 커야 함: {budget_ms}")
        #: `None` 이면 «판독기 없음» 이다. 예외가 아니라 기능 저하다 (모듈 주석 참조).
        self._factory = session_factory
        self._questions = tuple(questions)
        self._budget_ms = int(budget_ms)
        self._session: VlmSession | None = None

    @property
    def loaded(self) -> bool:
        return self._session is not None

    @property
    def available(self) -> bool:
        """적재를 시도할 수는 있는가. 팩토리가 없으면 거짓."""
        return self._factory is not None

    def load(self) -> bool:
        """모델을 올린다. 성공이면 참. **메인 루프에서 부르지 마라** (14.5초).

        ⚠️ **실패해도 예외를 올리지 않는다.** 가중치가 없는 것은 흔한 일이고
        (저장소에 넣지 않으므로) 그때 로봇이 서면 안 된다.
        """
        if self._session is not None:
            return True
        if self._factory is None:
            LOG.warning("vlm_unavailable", reason="no_factory")
            return False
        started = time.monotonic()
        try:
            self._session = self._factory()
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 기능 저하로 접는다
            LOG.warning("vlm_load_failed", error=type(exc).__name__, detail=str(exc))
            self._session = None
            return False
        LOG.info("vlm_loaded", elapsed_ms=int((time.monotonic() - started) * 1000))
        return True

    def unload(self) -> None:
        """모델을 내린다. **멱등이며 실패해도 조용히 지나간다.**

        ⚠️ 여기서 예외가 새면 종료(`Runtime.release`)가 뒤따르는 정리를 건너뛴다. 내리는 데
        실패한 VRAM 은 프로세스가 끝나면 풀린다 — 종료를 세워서 드러낼 일이 아니다.
        """
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("vlm_unload_failed", error=type(exc).__name__, detail=str(exc))
            return
        LOG.info("vlm_unloaded")

    def read(self, image: Any, *, now_ms: int, keys: Sequence[str] | None = None) -> Reading:
        """한 장을 읽는다. **절대 예외를 올리지 않는다.**

        예산(`budget_ms`)을 넘기면 남은 항목은 묻지 않고 거기까지 담아 돌려준다 —
        부분 판독이 빈 판독보다 낫다 (`Reading` 주석 참조).

        `keys` 를 주면 그 항목만 묻는다 — 공장 순찰 판독은 `person_down` 하나다 (S6).
        저하 여부도 물은 항목 수로 잰다.
        """
        if self._session is None:
            return Reading(answers=(), degraded=True, reason="not_loaded", taken_at_ms=now_ms)

        answers: list[Answer] = []
        spent_ms = 0
        reason: str | None = None
        questions = [q for q in self._questions if keys is None or q.key in keys]
        for question in questions:
            if spent_ms >= self._budget_ms:
                reason = "budget_exhausted"
                LOG.warning(
                    "vlm_budget", asked=len(answers), skipped=question.key, spent_ms=spent_ms
                )
                break
            started = time.monotonic()
            try:
                raw = self._session.ask(image, question.prompt)
            except Exception as exc:  # noqa: BLE001
                reason = "ask_failed"
                LOG.warning("vlm_ask_failed", key=question.key, error=type(exc).__name__)
                break
            elapsed_ms = int((time.monotonic() - started) * 1000)
            spent_ms += elapsed_ms
            value = parse_answer(raw)
            if value is None:
                # 답은 왔는데 읽지 못했다 — 모델이 서술로 답한 경우다. 원문을 남긴다.
                LOG.warning("vlm_unparsed", key=question.key, raw=raw[:120])
            answers.append(Answer(key=question.key, value=value, raw=raw, latency_ms=elapsed_ms))

        degraded = reason is not None or len(answers) < len(questions)
        return Reading(
            answers=tuple(answers),
            degraded=degraded,
            reason=reason,
            taken_at_ms=now_ms,
        )


__all__ = [
    "QUESTIONS",
    "Answer",
    "Question",
    "Reading",
    "VlmReader",
    "VlmSession",
    "parse_answer",
]
