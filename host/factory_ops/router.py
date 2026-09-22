"""현장지원 질의 라우터와 답변 조립 (WBS 4.7.16 · FR-12.1~12.4 · ADR-34).

**규칙표가 먼저 보고, 걸리지 않으면 이 모듈은 손을 뗀다.** `route()` 가 `None` 을
주면 그 질문은 운영정보가 아니며, 부르는 쪽이 안전 문서 RAG 나 LLM 경로로 보낸다.

⚠️ **LLM→SQL 경로가 없다** (ADR-34 ⓒ 기각 · 운용규칙 3). 질의로 정해지는 것은
조회군과 파라미터까지이고, 그 뒤는 `service.py` 의 바인딩 조회다. 자연어가 SQL 에
닿는 지점이 아예 없다.

⚠️ **숫자와 날짜를 문장으로 만드는 것도 코드다.** 조회 결과를 LLM 에 넘겨
«자연스럽게» 읽히면 *"약 300개"* 나 *"어제쯤"* 이 나온다. 아래 `_fmt_*` 가 결정론적
템플릿으로 찍고 출처와 갱신 시각을 꼬리에 붙인다 (`4.7.17` DoD ②).

⚠️ **조회가 실패해도 `None` 이 아니다.** 「자료 없음」과 「이 경로가 아님」을 같은
값으로 돌리면, 원본이 죽은 날 질문이 통째로 LLM 으로 흘러가 **그럴듯한 숫자가
나온다.** 실패는 문장으로 말한다.

⚠️ **조회군은 provider 로 갈린다.** 지금 등록된 것은 가상 MES(`mes`) 하나다. 로봇
텔레메트리(`4.7.10`)와 안전 문서 RAG 는 각자의 모듈이 자기 provider 를 넣는 자리이며
여기서 미리 만들어 두지 않는다. 없는 것을 부르는 코드가 생기면 «있는 척» 이 된다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple, Protocol

from host.factory_ops.service import SourceError, UnknownLineError

__all__ = [
    "CHECK_PROCEDURE",
    "HISTORY_KEYWORDS",
    "NO_HISTORY",
    "RULES",
    "SOURCE_KO",
    "UNAVAILABLE",
    "Provider",
    "Route",
    "answer",
    "route",
]

#: 운영정보를 못 주는 경우의 **유일한** 문구 (ADR-34 운용규칙 4 · `4.7.17` DoD ③).
#: 추정하지 않는다 — 여기서 값을 만들어 내면 TTL 을 둔 이유가 사라진다.
UNAVAILABLE = "최신 정보를 확인할 수 없습니다."

#: 고장·이상 질문에 붙이는 꼬리 (ADR-34 운용규칙 6 · `4.7.17` DoD ⑤).
#: **원인을 추정하지 않는다.** 지금 상태를 읽어 주고 절차로 넘긴다.
CHECK_PROCEDURE = "원인 진단은 하지 않습니다. 설비 매뉴얼의 점검 순서를 따르십시오."

#: 점검 «이력» 을 물었을 때 먼저 말하는 문장.
#:
#: ⚠️ **우리 자료는 «지금 상태» 뿐이다** (`4.7.15` DoD ②). 이력을 묻는 말에 현재
#: 상태만 돌려주면 듣는 사람은 그것을 이력으로 받아들인다. 없는 것은 없다고 먼저
#: 말한 뒤에 가진 것을 준다.
NO_HISTORY = "점검 이력은 보관하고 있지 않습니다."

#: 이력을 묻는 말. `RULES` 와 달리 조회군을 바꾸지 않고 답변 앞머리만 바꾼다.
HISTORY_KEYWORDS = ("설비이력", "점검이력", "점검기록")

#: 출처를 **소리 내어 읽을 때** 쓰는 표기.
#:
#: ⚠️ **기록에 남는 이름은 바꾸지 않는다** (FR-12.3). `store.SOURCE` 의 `demo-mes`
#: 는 로그와 대조용 식별자라 그대로 두고, 발화에서만 사람이 듣는 형태로 옮긴다.
#: TTS 가 `demo-mes` 를 읽으면 «데모 마이너스 엠이에스» 가 된다.
SOURCE_KO = {"demo-mes": "데모 MES"}


class Provider(Protocol):
    """조회군 하나를 책임지는 원본. `service.Service` 가 이 모양이다."""

    def query(
        self, endpoint: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


class Route(NamedTuple):
    """질의가 어디로 가는가. `group` 은 provider 이름이다."""

    group: str
    endpoint: str
    params: dict[str, Any]


#: 키워드 → 조회군 규칙표 (정본).
#:
#: `(keyword, group, endpoint, needs_line, attach_line)`
#:   needs_line  : 라인 표기(「A라인」)가 있을 때만 적용한다 — 「지금 상태 어때」를
#:                 생산 조회로 잡지 않기 위해서다
#:   attach_line : 라인 표기가 있으면 파라미터로 싣는다
#:
#: ⚠️ **순서가 곧 우선순위다.** 구체적인 키워드가 넓은 키워드보다 **앞에** 있어야
#: 한다. 그래서 넓은 말(`상태`·`가동`·`정지`)만 모아 표의 맨 끝에 둔다.
#:
#: ⚠️ **설비 규칙이 `상태` 보다 앞에 있어야 한다.** 뒤에 두면 「A라인 설비상태
#: 어때」가 `상태` 에 먼저 걸리고, 라인 표기가 있으니 `needs_line` 도 통과해서
#: **설비를 물었는데 생산 실적이 나온다.** 실제로 그렇게 잡히는 것을
#: `tests/test_factory_ops_router.py` 가 고정한다.
RULES: tuple[tuple[str, str, str, bool, bool], ...] = (
    # ── 생산: 구체 ───────────────────────────────────────────
    ("생산량", "mes", "production", False, True),
    ("생산현황", "mes", "production", False, True),
    ("생산목표", "mes", "production", False, True),
    ("목표대비", "mes", "production", False, True),
    ("진행률", "mes", "production", False, True),
    ("몇개만들", "mes", "production", False, True),
    ("가동중인라인", "mes", "production", False, False),
    ("어느라인", "mes", "production", False, False),
    ("라인상태", "mes", "production", False, True),
    ("라인현황", "mes", "production", False, True),
    ("가동현황", "mes", "production", False, True),
    # ── 출하 ────────────────────────────────────────────────
    ("출하일정", "mes", "shipments", False, False),
    ("배송일정", "mes", "shipments", False, False),
    ("출하", "mes", "shipments", False, False),
    ("납품", "mes", "shipments", False, False),
    ("납기", "mes", "shipments", False, False),
    # ── 작업지시 ─────────────────────────────────────────────
    ("작업지시", "mes", "schedule", False, True),
    ("작업스케줄", "mes", "schedule", False, True),
    ("작업순서", "mes", "schedule", False, True),
    ("지시서", "mes", "schedule", False, True),
    ("우선순위", "mes", "schedule", False, True),
    # ── 설비: 넓은 말보다 반드시 앞 ───────────────────────────
    ("설비상태", "mes", "equipment", False, True),
    ("설비현황", "mes", "equipment", False, True),
    ("설비점검", "mes", "equipment", False, True),
    ("설비이력", "mes", "equipment", False, True),
    ("점검이력", "mes", "equipment", False, True),
    ("점검기록", "mes", "equipment", False, True),
    ("설비", "mes", "equipment", False, True),
    ("고장", "mes", "equipment", False, True),
    # ── 넓은 말: 라인 표기가 있을 때만 ────────────────────────
    ("가동", "mes", "production", True, True),
    ("돌아가", "mes", "production", True, True),
    ("멈춰", "mes", "production", True, True),
    ("세워", "mes", "production", True, True),
    ("정지", "mes", "production", True, True),
    ("상태", "mes", "production", True, True),
)

_LINE_RE = re.compile(r"([A-Za-z])라인")


def _norm(text: str) -> str:
    return re.sub(r"[\s,.!?~…:'\"·]+", "", text)


def _line_of(norm: str) -> str | None:
    match = _LINE_RE.search(norm)
    return match.group(1).upper() if match else None


def route(text: str) -> Route | None:
    """질의 → 조회군. **모르면 `None`** 이고 그 질문은 여기서 끝이 아니다."""
    norm = _norm(text)
    line = _line_of(norm)
    for keyword, group, endpoint, needs_line, attach_line in RULES:
        if keyword in norm and (not needs_line or line):
            return Route(group, endpoint, {"line": line} if attach_line and line else {})
    return None


# ── 결정론적 답변 템플릿 ──────────────────────────────────────


def _euro(word: str) -> str:
    """조사 «으로/로» 를 고른다.

    ⚠️ **ㄹ 받침은 «로» 다** (종성 8). 받침 유무만 보면 「한국정밀으로」가 나오고
    TTS 가 그대로 읽는다.
    """
    last = ord(word[-1]) - 0xAC00 if word else 0
    jong = last % 28 if 0 <= last < 11172 else 0
    return "으로" if jong and jong != 8 else "로"


def _ko_date(iso: Any) -> str:
    """`2026-09-18` → `9월 18일`. TTS 가 자연스럽게 읽는 형태다."""
    try:
        _y, month, day = str(iso)[:10].split("-")
        return f"{int(month)}월 {int(day)}일"
    except ValueError:
        return str(iso)


def _source_tail(row: Mapping[str, Any], dropped: int) -> str:
    """«데모 MES 의 13시 42분 자료입니다» + 제외 건수 (`4.7.17` DoD ②)."""
    try:
        hour, minute = str(row["updated_at"])[11:16].split(":")
        source = str(row.get("source", "MES"))
        spoken = SOURCE_KO.get(source, source)
        tag = f"{spoken}의 {int(hour)}시 {int(minute)}분 자료입니다."
    except (KeyError, IndexError, ValueError):
        tag = ""
    note = f"오래된 자료 {dropped}건은 답변에서 제외했습니다." if dropped else ""
    return " ".join(part for part in (tag, note) if part)


def _fresh_only(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    fresh = [row for row in rows if row.get("fresh")]
    return fresh, len(rows) - len(fresh)


def _stale(what: str) -> str:
    return f"{what}의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"


def _fmt_production(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "등록된 생산 라인이 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return _stale("생산 정보")
    state_ko = {"running": "가동 중", "stopped": "정지", "idle": "대기"}
    parts = [
        f"{row['line_id']}라인은 목표 {row['target_quantity']:,}개 중"
        f" {row['completed_quantity']:,}개를 완료해 {row['remaining_quantity']:,}개 남았고"
        f" 현재 {state_ko.get(row['state'], row['state'])}입니다"
        for row in rows
    ]
    return ". ".join(parts) + f". {_source_tail(rows[0], dropped)}"


def _fmt_shipments(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "앞으로 7일 이내 출하 예정이 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return _stale("출하 일정")
    parts = [
        f"{_ko_date(row['deadline'])}에 {row['customer']}{_euro(row['customer'])}"
        f" {row['product']} {row['quantity']:,}개"
        for row in rows[:4]
    ]
    head = ", ".join(parts)
    if len(rows) > 4:
        head += f" 외 {len(rows) - 4}건"
    return f"예정된 출하가 {len(rows)}건 있습니다. {head}. {_source_tail(rows[0], dropped)}"


def _fmt_schedule(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "진행 중이거나 대기 중인 작업지시가 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return _stale("작업지시")
    top = rows[0]
    status_ko = {"in_progress": "진행 중", "pending": "대기"}
    text = (
        f"가장 급한 작업은 {top['line_id']}라인 '{top['description']}'이고"
        f" {status_ko.get(top['status'], top['status'])}입니다"
    )
    if len(rows) > 1:
        text += f". 나머지 작업지시가 {len(rows) - 1}건 있습니다"
    return text + f". {_source_tail(top, dropped)}"


def _fmt_equipment(rows: list[dict[str, Any]]) -> str:
    """설비는 **지금 상태**를 답한다 (`4.7.15` DoD ②).

    ⚠️ **점검 결과가 아니라 가동 상태다.** 「지금 프레스-01 돌아가나요」에 답할 수
    있어야 하므로 `running`·`idle`·`maintenance`·`fault` 를 그대로 읽어 준다.
    """
    if not rows:
        return "등록된 설비가 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return _stale("설비 상태")
    state_ko = {
        "running": "가동 중",
        "idle": "대기",
        "maintenance": "정비 중",
        "fault": "고장",
    }
    # 정비 중과 고장만 따로 말한다 — 가동 중인 설비를 전부 읽으면 답이 길어지고
    # 정작 손봐야 할 설비가 묻힌다.
    bad = [row for row in rows if row["state"] in ("maintenance", "fault")]
    tail = _source_tail(rows[0], dropped)
    if not bad:
        return f"설비 {len(rows)}대가 모두 정상입니다. {tail}"
    parts = [
        f"{row['label']}({row['line_id']}라인) {state_ko.get(row['state'], row['state'])}"
        for row in bad
    ]
    return (
        f"주의가 필요한 설비가 {len(bad)}대 있습니다. "
        + ", ".join(parts)
        + f". {CHECK_PROCEDURE} {tail}"
    )


_FORMAT = {
    "production": _fmt_production,
    "shipments": _fmt_shipments,
    "schedule": _fmt_schedule,
    "equipment": _fmt_equipment,
}


def answer(text: str, providers: Mapping[str, Provider]) -> str | None:
    """운영정보 질의면 답 문장, 아니면 `None` (= 이 경로가 아니다)."""
    hit = route(text)
    if hit is None:
        return None
    provider = providers.get(hit.group)
    if provider is None:
        return UNAVAILABLE
    try:
        rows = provider.query(hit.endpoint, hit.params)
    except UnknownLineError as exc:
        listed = ", ".join(exc.valid)
        return (
            f"그 라인은 등록되어 있지 않습니다. 등록된 라인은 {listed}입니다."
            if listed
            else "등록된 라인이 없습니다."
        )
    except SourceError:
        return UNAVAILABLE

    said = _FORMAT[hit.endpoint](rows)
    if hit.endpoint == "equipment" and any(k in _norm(text) for k in HISTORY_KEYWORDS):
        return f"{NO_HISTORY} {said}"
    return said
