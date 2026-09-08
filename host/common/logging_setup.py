"""JSON Lines 로거 (WBS 4.4.2 · NFR-3④ · ENGINEERING_GUIDE 1절).

로그의 목적은 *"동작했다"* 가 아니라 **"왜 그때 그 판단을 했는가"** 에 답하는 것이다.
그것이 규칙 기반 FSM 을 고른 실질적 근거이므로(DR-9), 답할 수 없는 로그는 FSM 을
고른 이유를 무효로 만든다.

**컨텍스트를 사람이 기억해서 넣는 구조로는 반드시 빠진다.** 그래서 세 가지를 코드로
강제한다.

1. **필수 컨텍스트는 필터가 넣는다** — 호출부는 `log.info("fsm_transition", **detail)`
   만 쓰고 `device_id`·`seq`·`state`·`escalation` 은 `LogContext` 에서 자동으로 실린다.
2. **빠진 레코드는 시험이 잡는다** — 골든 픽스처(`log_samples.jsonl`)를 두고 통신
   규약과 같은 방식으로 대조한다.
3. **샘플링도 도구로 강제한다** — `EdgeTrigger` 와 `PeriodicSummary` 없이 매 프레임
   찍으면 15fps × 10분 = 9,000줄이 되고 **정작 중요한 이벤트가 묻힌다.**

⚠️ **파일은 JSON Lines, 콘솔은 사람이 읽는 형식**으로 나눈다. 한쪽만 두면 둘 중 하나가
불편해진다 — 기계가 읽을 것과 사람이 볼 것은 목적이 다르다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 전 레코드 필수 컨텍스트 (ENGINEERING_GUIDE 1.1). **6개다.**
#:
#: 하나라도 없으면 사후 재구성이 불가능해진다 — `device_id` 가 없으면 3대 중 누구인지
#: 모르고, `seq` 가 없으면 어느 텔레메트리를 보고 내린 판단인지 짚을 수 없다.
REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"ts", "level", "device_id", "seq", "state", "escalation"}
)

#: 레벨 정책 (ENGINEERING_GUIDE 1.2). 파이썬의 `WARNING` 을 `WARN` 으로 적는다 —
#: 문서·픽스처가 `WARN` 이므로 정본을 따른다.
LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})

_LEVEL_NAMES = {"WARNING": "WARN"}

#: 로그 레코드가 아니라 표준 로깅이 붙이는 필드. `detail` 로 새지 않게 걸러낸다.
_LOGRECORD_KEYS: frozenset[str] = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))

#: 상태에서 곧바로 유도되는 에스컬레이션 단계.
#:
#: ⚠️ **임시다.** `L1`~`L3` 는 대응 강도 축이고 `3.8.3` 이 채운다. 다만 페일세이프는
#: 에스컬레이션 표에서 `F` 로 이미 정해져 있고(아키텍처 3.1), 로봇이 쓰러져 있는데
#: 로그에 `L0`(정상 순찰)이 찍히면 **로그가 거짓을 말한다.** 그래서 이 하나만 유도한다.
BASELINE_ESCALATION: dict[str, str] = {"FAILSAFE": "F"}
DEFAULT_ESCALATION = "L0"

#: 진입 자체가 **기능 상실**인 상태 — 레벨 정책의 `ERROR` 행이다 (1.2).
#:
#: 상태 이름을 호출부에 두지 않기 위해 여기 표로 둔다. `LOST`(측위 상실)는 Phase 2
#: 이므로 그때 넣는다.
ERROR_ON_ENTER: frozenset[str] = frozenset({"FAILSAFE"})


@dataclass
class LogContext:
    """모든 레코드에 실릴 공통 컨텍스트. **운용 루프가 갱신하고 필터가 읽는다.**

    가변 객체인 것이 의도다 — 로거를 새로 만들지 않고 이 하나를 고쳐서 이후 모든
    레코드에 반영한다. 호출부가 매번 컨텍스트를 넘기는 구조로는 반드시 빠진다.
    """

    device_id: str = "unknown"
    #: 판단의 근거가 된 **마지막 수락 텔레메트리의 seq**.
    #:
    #: 명령 seq 가 아니라 텔레메트리 seq 인 이유 — 로그를 뒤지는 목적은 *"무엇을 보고
    #: 이 판단을 했나"* 이고, 그 답은 로봇이 보낸 레코드다. 명령 seq 가 필요한 자리는
    #: `detail` 에 함께 싣는다.
    seq: int = 0
    state: str = "IDLE"
    escalation: str = DEFAULT_ESCALATION

    def observe(self, *, seq: int | None = None, state: str | None = None) -> None:
        """수신·전이 때마다 부른다. 에스컬레이션은 상태에서 유도한다."""
        if seq is not None:
            self.seq = seq
        if state is not None:
            self.state = state
            self.escalation = BASELINE_ESCALATION.get(state, DEFAULT_ESCALATION)

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "seq": self.seq,
            "state": self.state,
            "escalation": self.escalation,
        }


class ContextFilter(logging.Filter):
    """공통 컨텍스트를 레코드에 붙인다. **호출부가 잊을 수 없는 유일한 방법이다.**

    ⚠️ **로거가 아니라 핸들러에 붙여야 한다.** 로거에 붙인 필터는 그 로거로 직접
    들어온 레코드에만 적용되고 **자식 로거에서 전파된 레코드에는 적용되지 않는다.**
    호출부는 `mechadog.runtime` 처럼 자식 이름을 쓰므로, `mechadog` 에 붙이면
    컨텍스트가 하나도 실리지 않는다 — 처음 그렇게 만들었고 시험이 잡았다.
    """

    def __init__(self, context: LogContext) -> None:
        super().__init__()
        self._context = context

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self._context.as_dict().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonlFormatter(logging.Formatter):
    """한 줄에 한 레코드. **`ts` 는 프로토콜과 같은 epoch 밀리초다.**

    ISO 문자열을 쓰지 않는 이유 — 텔레메트리·명령의 `ts` 와 같은 축이어야 로그와
    패킷을 같은 시간선에 놓을 수 있다. 사람이 읽는 시각은 콘솔 쪽이 담당한다.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(record.created * 1000),
            "level": _LEVEL_NAMES.get(record.levelname, record.levelname),
            "device_id": getattr(record, "device_id", "unknown"),
            "seq": getattr(record, "seq", 0),
            "state": getattr(record, "state", "IDLE"),
            "escalation": getattr(record, "escalation", DEFAULT_ESCALATION),
            "event": getattr(record, "event", record.getMessage()),
        }
        detail = getattr(record, "detail", None)
        if detail:
            payload["detail"] = detail
        for key in ("track_id", "zone"):  # 상황별 추가 컨텍스트 (1.1)
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["detail"] = {
                **(detail or {}),
                "exc": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class ConsoleFormatter(logging.Formatter):
    """사람이 보는 한 줄. 기계가 읽을 것과 목적이 달라 따로 둔다."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        detail = getattr(record, "detail", None)
        if event is not None:
            bits = " ".join(f"{k}={v}" for k, v in (detail or {}).items())
            state = getattr(record, "state", "")
            record.msg = f"[{state}] {event}" + (f" · {bits}" if bits else "")
            record.args = ()
        return super().format(record)


class EventLogger(logging.LoggerAdapter):
    """`log.info("fsm_transition", **detail)` 형태로 쓴다.

    표준 로거는 임의 키워드를 받지 않으므로 어댑터로 감싼다. 이렇게 두면 호출부가
    **사건 이름과 근거만** 적게 되고, 문장을 만들지 않으므로 로그가 기계로 읽힌다.
    """

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(logger, {})

    def _emit(self, level: int, event: str, **detail: Any) -> None:
        if not self.logger.isEnabledFor(level):
            return  # 문자열·사전을 만들기 전에 끊는다 (DEBUG 가 매 프레임 도는 자리)
        extra: dict[str, Any] = {"event": event}
        for key in ("track_id", "zone"):
            if key in detail:
                extra[key] = detail.pop(key)
        clean = {k: v for k, v in detail.items() if k not in _LOGRECORD_KEYS}
        if clean:
            extra["detail"] = clean
        self.logger.log(level, event, extra=extra)

    def debug(self, event: str, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.DEBUG, event, **detail)

    def info(self, event: str, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.INFO, event, **detail)

    def warning(self, event: str, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.WARNING, event, **detail)

    def error(self, event: str, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.ERROR, event, **detail)


def event_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))


# ── 샘플링 (ENGINEERING_GUIDE 1.3) ────────────────────────────
class EdgeTrigger:
    """값이 **변할 때만** 참을 낸다. `PATROL 유지 중` 을 9천 번 남기지 않는다."""

    def __init__(self) -> None:
        self._last: dict[str, Any] = {}

    def changed(self, key: str, value: Any) -> bool:
        if key in self._last and self._last[key] == value:
            return False
        self._last[key] = value
        return True

    def forget(self, key: str) -> None:
        """다음 값을 무조건 변화로 보게 한다 (재연결 등 경계에서 쓴다)."""
        self._last.pop(key, None)


@dataclass
class PeriodicSummary:
    """순간 이벤트를 카운터로 모아 **주기마다 한 줄**로 낸다.

    시각을 인자로 받는다 — 링크 감시·타이머와 같은 방식이며, 1초 요약을 실제로
    1초 기다리지 않고 시험할 수 있다.
    """

    interval_ms: int = 1000
    _counters: dict[str, float] = field(default_factory=dict)
    _due_ms: int | None = None

    def count(self, name: str, amount: float = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, value: float) -> None:
        """평균을 낼 값. 합과 개수를 함께 모은다."""
        self.count(f"{name}_sum", value)
        self.count(f"{name}_n")

    def drain(self, now_ms: int) -> dict[str, float] | None:
        """주기가 됐으면 집계를 돌려주고 카운터를 비운다. 아니면 `None`.

        **비우는 것이 요점이다.** 누적을 그대로 두면 요약이 "지금까지 총합" 이 되어
        구간별 이상을 볼 수 없다.
        """
        if self._due_ms is None:
            self._due_ms = now_ms + self.interval_ms
            return None
        if now_ms < self._due_ms:
            return None
        self._due_ms += self.interval_ms
        if self._due_ms <= now_ms:  # 크게 밀렸으면 재동기 (몰아 내지 않는다)
            self._due_ms = now_ms + self.interval_ms
        out: dict[str, float] = {}
        for name, total in self._counters.items():
            if name.endswith("_sum"):
                base = name[: -len("_sum")]
                n = self._counters.get(f"{base}_n", 0)
                out[f"{base}_avg"] = round(total / n, 2) if n else 0.0
            elif not name.endswith("_n"):
                out[name] = total
        self._counters.clear()
        return out


# ── 검증 (골든 픽스처가 물린다) ───────────────────────────────
def record_error(obj: Mapping[str, Any]) -> str:
    """레코드 하나를 검사한다. 문제가 없으면 빈 문자열.

    통신 규약의 디코더와 같은 모양이다 — 예외를 던지지 않고 **사유를 돌려준다.**
    로그를 검사하다가 프로그램이 죽으면 안 되고, 시험은 사유를 읽어야 한다.
    """
    missing = sorted(REQUIRED_FIELDS - set(obj))
    if missing:
        return f"필수 컨텍스트 누락: {', '.join(missing)}"
    if obj["level"] not in LEVELS:
        return f"알 수 없는 레벨: {obj['level']!r}"
    if not isinstance(obj["ts"], int) or obj["ts"] < 0:
        return "ts 가 epoch 밀리초 정수가 아님"
    if not isinstance(obj["seq"], int) or obj["seq"] < 0:
        return "seq 가 0 이상 정수가 아님"
    if not isinstance(obj["device_id"], str) or not obj["device_id"]:
        return "device_id 가 비어 있음"
    if not isinstance(obj.get("event"), str) or not obj["event"]:
        return "event 이름이 없음"
    if "detail" in obj and not isinstance(obj["detail"], dict):
        return "detail 이 객체가 아님"
    return ""


# ── 조립 ─────────────────────────────────────────────────────
def setup_logging(
    config: Mapping[str, Any],
    *,
    device_id: str,
    context: LogContext | None = None,
    log_dir: Path | None = None,
    console: bool = True,
) -> LogContext:
    """설정대로 핸들러를 붙이고 컨텍스트를 돌려준다.

    레벨·회전 크기·보관 개수·디렉터리 전부 `config.logging` 에서 온다 (NFR-3①).
    **여기서 숫자를 기본값으로 채우지 않는다** — 채우면 설정을 고쳐도 안 바뀐다.
    """
    section = config["logging"]
    ctx = context if context is not None else LogContext()
    ctx.device_id = device_id

    root = logging.getLogger("mechadog")
    root.setLevel(str(section["level"]).upper())
    root.propagate = False
    for existing in list(root.handlers):  # 두 번 부르면 줄이 두 번 찍힌다
        root.removeHandler(existing)
        existing.close()
    context_filter = ContextFilter(ctx)

    directory = log_dir if log_dir is not None else Path(str(section["dir"]))
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        directory / f"{device_id}.jsonl",
        maxBytes=int(section["rotate_mb"]) * 1024 * 1024,
        backupCount=int(section["rotate_keep"]),
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonlFormatter())
    file_handler.addFilter(context_filter)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(ConsoleFormatter())
        stream.addFilter(context_filter)
        root.addHandler(stream)
    return ctx
