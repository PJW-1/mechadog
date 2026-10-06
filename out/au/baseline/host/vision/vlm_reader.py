"""로컬 VLM 상황 판독 (FR-8 · ADR-35).

COCO 목록으로는 «쓰러져 있다» 를 말할 수 없어 사진을 읽는 모델을 두되, 판정은 맡기지 않는다.

    ① QUESTIONS      고정 영어 닫힌 질문 항목 — 데이터일 뿐이다
    ② parse_answer   모델이 뱉은 글 → 참/거짓/모름. 모델·GPU 없이 시험된다
    ③ VlmReader      적재·해제·질의. 세션은 주입받는다

- 관찰만 돌려주고 사건 이름을 만들지 않는다 — 사건은 FSM 이 만든다 (ADR-35 결정 2).
- 영어로 묻는다 — 한국어 질문에는 2B 모델이 틀렸다 (ADR-35 결정 3).
- 가중치가 없거나 실패해도 예외 없이 기능 저하로 기록한다 (Tier 3 · ADR-35 결정 6).
- ⚠️ `load()`(약 14.5초)는 운용 루프 스레드에서 부르지 않는다 — 로봇은 `safety.cmd_timeout_ms`
  동안 명령을 못 받으면 선다.
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
    """물어볼 항목 하나. `prompt` 는 «yes or no» 로 닫은 영어 문장이다."""

    #: 결과를 찾아 쓰는 키. 로그와 사건 쪽에서 쓰는 이름이다.
    key: str
    #: 모델에게 그대로 가는 문장(번역하지 않는다).
    prompt: str
    #: 이 항목이 무엇을 보려는 것인지. 사람이 읽는 자리다.
    intent: str


#: 고정 질문 셋 — 모두 «본 것» 을 묻는다. «위험한가» 류 열린 질문은 넣지 않는다 (ADR-35 결정 2).
#: 질문 하나마다 판독 시간이 붙는다 — 그래서 부르는 쪽이 `keys` 로 필요한 것만 묻는다
#: (`hazard_item` 은 화기 위험구역에서만 · `ZoneInspector`).
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
    Question(
        key="hazard_item",
        prompt="Is there a lighter or a power bank in this image? Answer with yes or no only.",
        intent="화기 위험구역의 위험물(라이터·보조배터리) — COCO 어휘 밖",
    ),
)

#: 답 앞머리에서 찾는 토큰 (`parse_answer`).
_YES = frozenset({"yes", "yeah", "yep"})
_NO = frozenset({"no", "nope", "none"})


@dataclass(frozen=True, slots=True)
class Answer:
    """항목 하나의 판독. `value` 는 참·거짓·모름(`None`)의 3값이다 — 모름을 거짓으로 접지 않는다."""

    key: str
    #: 참/거짓, 또는 읽어 내지 못했으면 `None`
    value: bool | None
    #: 모델이 실제로 뱉은 글
    raw: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class Reading:
    """한 프레임의 판독 묶음. 예산을 넘겨 못 물은 항목은 빼고 `degraded` 를 세운다(부분 판독)."""

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
    """적재된 모델 한 벌 — 주입받는다(이 모듈은 파일도 GPU 도 만지지 않는다)."""

    def ask(self, image: Any, prompt: str) -> str:
        """이미지 한 장에 질문 하나. 답을 글로 돌려준다."""
        ...

    def close(self) -> None:
        """VRAM 을 놓는다."""
        ...


def parse_answer(text: str) -> bool | None:
    """모델의 글에서 참/거짓을 읽는다. 못 읽으면 `None`.

    닫힌 질문의 답은 앞에 오므로 첫 단어 하나만 토큰으로 견준다 — 뒤쪽의 부정어나
    `not`·`nothing` 같은 접두 일치에 뒤집히지 않는다.
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
    """판독기 수명주기 — 기동 때 한 번 올려 상시 적재하고 종료 때 내린다 (ADR-35 결정 5).

    ⚠️ `load()` 는 멱등이지만 스레드 안전하지 않다 — 적재 중에 또 부르면 두 벌(VRAM 초과)을
    올린다. `VlmWorker.start()` 한 곳에서만 부른다.
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
        #: `None` 이면 «판독기 없음» 이다 — 예외가 아니라 기능 저하다.
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
        """모델을 올린다. 성공이면 참, 실패는 예외 없이 거짓이다. 운용 루프에서 부르지 않는다."""
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
        """모델을 내린다. 멱등이며 실패해도 예외를 올리지 않는다 — 종료 정리를 끊지 않는다."""
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
        """한 장을 읽는다. 예외를 올리지 않는다.

        예산(`budget_ms`)을 넘기면 남은 항목은 묻지 않고 거기까지 돌려준다. `keys` 를 주면
        그 항목만 묻는다(공장 순찰은 `person_down` 하나 · ADR-42 결정 5).
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
                # 답은 왔는데 읽지 못했다 — 원문을 남긴다.
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
