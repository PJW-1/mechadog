"""JSON Lines 로거 (WBS 4.4.2 · NFR-3④ · ENGINEERING_GUIDE 1절).

로그는 «왜 그때 그 판단을 했는가» 에 답해야 한다 — 규칙 기반 FSM 을 고른 근거다(ADR-14).

1. 필수 컨텍스트(`device_id`·`seq`·`state`·`escalation`·`mode`)는 `ContextFilter` 가
   `LogContext` 에서 넣는다. 호출부는 `log.info("fsm_transition", **detail)` 만 쓴다.
2. 레코드 형식은 골든 픽스처(`log_samples.jsonl`)로 시험한다.
3. 매 프레임 이벤트는 `EdgeTrigger`·`PeriodicSummary` 로 줄인다.

파일은 JSON Lines, 콘솔은 사람이 읽는 한 줄이다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host.common.config import repo_path

#: 전 레코드 필수 컨텍스트 (ENGINEERING_GUIDE 1.1).
REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"ts", "level", "device_id", "seq", "state", "escalation"}
)

#: 레벨 정책 (ENGINEERING_GUIDE 1.2). 파이썬의 `WARNING` 은 문서·픽스처대로 `WARN` 으로 적는다.
LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})

_LEVEL_NAMES = {"WARNING": "WARN"}

#: 로그 레코드가 아니라 표준 로깅이 붙이는 필드. `detail` 로 새지 않게 걸러낸다.
_LOGRECORD_KEYS: frozenset[str] = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))

#: `bind_escalation()` 을 하지 않았을 때 상태에서 유도하는 단계. 정본은 `Escalation`(3.8.3)
#: 이고, 유도하는 것은 `FAILSAFE → F` 하나다(아키텍처 3.1).
BASELINE_ESCALATION: dict[str, str] = {"FAILSAFE": "F"}
DEFAULT_ESCALATION = "L0"

#: 운용 모드를 연결하지 않았을 때의 값 (FR-11.2 기본값). 모드 목록의 정본은 `behavior/mission.py`.
DEFAULT_MODE = "guard"

#: 진입 자체가 기능 상실인 상태 — 레벨 정책의 `ERROR` 행이다 (1.2).
ERROR_ON_ENTER: frozenset[str] = frozenset({"FAILSAFE"})


@dataclass
class LogContext:
    """모든 레코드에 실릴 공통 컨텍스트. 운용 루프가 고치고 `ContextFilter` 가 읽는 가변 객체다."""

    device_id: str = "unknown"
    #: 판단 근거가 된 마지막 수락 **텔레메트리** seq (명령 seq 는 필요하면 `detail` 에 싣는다).
    seq: int = 0
    state: str = "IDLE"
    escalation: str = DEFAULT_ESCALATION
    #: 운용 모드 (FR-11.5 · ADR-33 규칙 5). 관제 화면에서도 바뀌므로 `mode_source` 로 묻는다.
    mode: str = DEFAULT_MODE
    mode_source: Callable[[], str] | None = field(default=None, repr=False)
    #: 대응 단계의 정본. 단계는 사건·시간·확인으로 바뀌므로 레코드마다 지금 값을 묻는다.
    escalation_source: Callable[[], str] | None = field(default=None, repr=False)

    def bind_escalation(self, source: Callable[[], str]) -> None:
        """대응 단계의 정본을 연결한다 (`3.8.3`). **연결되면 상태 유도를 쓰지 않는다.**"""
        self.escalation_source = source

    def bind_mode(self, source: Callable[[], str]) -> None:
        """운용 모드의 정본을 연결한다 (`3.4.4` · FR-11.5)."""
        self.mode_source = source

    def observe(self, *, seq: int | None = None, state: str | None = None) -> None:
        """수신·전이 때마다 부른다. 단계가 연결되지 않았으면 상태에서 유도한다."""
        if seq is not None:
            self.seq = seq
        if state is not None:
            self.state = state
        if self.escalation_source is not None:
            self.escalation = self.escalation_source()
        elif state is not None:
            self.escalation = BASELINE_ESCALATION.get(state, DEFAULT_ESCALATION)
        if self.mode_source is not None:
            self.mode = self.mode_source()

    def as_dict(self) -> dict[str, Any]:
        """레코드마다 불린다. 연결된 정본에서 단계·모드를 다시 묻고 필드에도 되써 둔다."""
        if self.escalation_source is not None:
            self.escalation = self.escalation_source()
        if self.mode_source is not None:
            self.mode = self.mode_source()
        return {
            "device_id": self.device_id,
            "seq": self.seq,
            "state": self.state,
            "escalation": self.escalation,
            "mode": self.mode,
        }


class ContextFilter(logging.Filter):
    """공통 컨텍스트를 레코드에 붙인다.

    로거가 아니라 핸들러에 붙인다 — 로거 필터는 자식 로거(`mechadog.runtime` 등)에서
    전파된 레코드에 적용되지 않는다.
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
    """한 줄에 한 레코드. `ts` 는 패킷과 같은 시간축에 놓이도록 epoch 밀리초다."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(record.created * 1000),
            "level": _LEVEL_NAMES.get(record.levelname, record.levelname),
            "device_id": getattr(record, "device_id", "unknown"),
            "seq": getattr(record, "seq", 0),
            "state": getattr(record, "state", "IDLE"),
            "escalation": getattr(record, "escalation", DEFAULT_ESCALATION),
            "mode": getattr(record, "mode", DEFAULT_MODE),
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
    """`log.info("fsm_transition", **detail)` 형태로 사건 이름과 근거만 싣는 어댑터."""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__(logger, {})

    def _emit(self, level: int, event: str, /, **detail: Any) -> None:
        """`level`·`event` 는 위치 전용이다 — 같은 이름의 근거 키워드를 실어도 `TypeError` 가 나지 않는다."""
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

    def debug(self, event: str, /, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.DEBUG, event, **detail)

    def info(self, event: str, /, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.INFO, event, **detail)

    def warning(self, event: str, /, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.WARNING, event, **detail)

    def error(self, event: str, /, **detail: Any) -> None:  # type: ignore[override]
        self._emit(logging.ERROR, event, **detail)


def event_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))


# ── 샘플링 (ENGINEERING_GUIDE 1.3) ────────────────────────────
class EdgeTrigger:
    """값이 변할 때만 참을 낸다."""

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
    """순간 이벤트를 카운터로 모아 주기마다 한 줄로 낸다. 시각은 인자로 받는다."""

    interval_ms: int = 1000
    _counters: dict[str, float] = field(default_factory=dict)
    _due_ms: int | None = None

    def count(self, name: str, amount: float = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, value: float) -> None:
        """평균을 낼 값. 합과 개수를 함께 모은다."""
        self.count(f"{name}_sum", value)
        self.count(f"{name}_n")

    def maximum(self, name: str, value: float) -> None:
        """구간 최댓값. 주기 위반은 평균이 아니라 최댓값에 드러난다 (ADR-39)."""
        key = f"{name}_max"
        previous = self._counters.get(key)
        if previous is None or value > previous:
            self._counters[key] = value

    def drain(self, now_ms: int) -> dict[str, float] | None:
        """주기가 됐으면 이번 구간의 집계를 돌려주고 카운터를 비운다. 아니면 `None`."""
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
    """레코드 하나를 검사해 사유를 돌려준다(예외 없음). 문제가 없으면 빈 문자열."""
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

    레벨·회전 크기·보관 개수·디렉터리 전부 `config.logging` 에서 오고 기본값을 채우지 않는다 (NFR-3①).
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

    directory = log_dir if log_dir is not None else repo_path(str(section["dir"]))
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
