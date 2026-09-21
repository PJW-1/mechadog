"""질의 라우터 + 답변 조립 (WBS 4.7.16 · ADR-34).

**규칙표가 먼저 보고, 걸리지 않으면 이 모듈은 손을 뗀다.** `route()` 가 `None`
을 주면 그 질문은 운영정보가 아니며 호출자가 안전 문서·LLM 경로로 보낸다.

⚠️ **LLM→SQL 경로가 없다** (ADR-34 ⓒ 기각 · 운용규칙 3). 질의는 키워드 규칙표로
**조회군과 파라미터**까지만 정해지고, 그 뒤는 `service.py` 의 바인딩 조회다.
자연어가 SQL 에 닿는 지점이 아예 없다.

⚠️ **숫자와 날짜를 문장으로 만드는 것도 코드다.** 조회 결과를 LLM 에 넘겨
«자연스럽게» 읽히면 *"약 300개"* 나 *"어제쯤"* 이 나온다. 아래 `_fmt_*` 가
결정론적 템플릿으로 찍고, 출처와 갱신 시각을 꼬리에 붙인다 (`4.7.17` DoD ②).

⚠️ **조회군은 provider 로 갈린다.** 지금 등록된 것은 가상 MES(`mes`) 하나다.
로봇 텔레메트리(`4.7.10`)와 안전 문서 RAG 는 **각자의 모듈이 자기 provider 를
넣는 자리**이며, 여기서 미리 만들어 두지 않는다 — 없는 것을 부르는 코드가
생기면 «있는 척» 이 시작된다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple, Protocol

from host.factory_ops.service import SourceError, UnknownLineError

#: 운영정보를 못 주는 경우의 **유일한** 문구 (ADR-34 운용규칙 4 · `4.7.17` DoD ③).
#: 추정하지 않는다 — 여기서 값을 만들어 내면 TTL 을 둔 이유가 사라진다.
UNAVAILABLE = "최신 정보를 확인할 수 없습니다."

#: 고장 질문에 붙이는 꼬리 (ADR-34 운용규칙 6 · `4.7.17` DoD ⑤).
#: **원인을 추정하지 않는다** — 점검 기록을 읽어 주고 절차로 넘긴다.
CHECK_PROCEDURE = "원인 진단은 하지 않습니다. 설비 매뉴얼의 점검 순서를 따르십시오."


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
#:   needs_line  : 라인 표기(「A라인」)가 있을 때만 적용 — 「지금 상태 어때」를
#:                 생산 조회로 잡지 않기 위해서다
#:   attach_line : 라인 표기가 있으면 파라미터로 싣는다
#:
#: ⚠️ **순서가 우선순위다.** 구체적인 키워드가 넓은 키워드보다 **앞에** 있어야
#: 한다 — 「라인상태」가 「상태」 뒤에 오면 영원히 안 걸린다.
RULES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("생산량", "mes", "production", False, True),
    ("생산현황", "mes", "production", False, True),
    ("생산목표", "mes", "production", False, True),
    ("목표대비", "mes", "production", False, True),
    ("진행률", "mes", "production", False, True),
    ("몇개만들", "mes", "production", False, True),
    ("라인상태", "mes", "production", False, False),
    ("라인현황", "mes", "production", False, False),
    ("가동현황", "mes", "production", False, False),
    ("가동중인라인", "mes", "production", False, False),
    ("어느라인", "mes", "production", False, False),
    ("가동", "mes", "production", True, True),
    ("돌아가", "mes", "production", True, True),
    ("멈춰", "mes", "production", True, True),
    ("세워", "mes", "production", True, True),
    ("정지", "mes", "production", True, True),
    ("상태", "mes", "production", True, True),
    ("출하", "mes", "shipments", False, False),
    ("납품", "mes", "shipments", False, False),
    ("납기", "mes", "shipments", False, False),
    ("배송일정", "mes", "shipments", False, False),
    ("출하일정", "mes", "shipments", False, False),
    ("작업지시", "mes", "schedule", False, True),
    ("지시서", "mes", "schedule", False, True),
    ("작업순서", "mes", "schedule", False, True),
    ("작업스케줄", "mes", "schedule", False, True),
    ("우선순위", "mes", "schedule", False, True),
    ("설비점검", "mes", "equipment", False, True),
    ("설비상태", "mes", "equipment", False, True),
    ("설비이력", "mes", "equipment", False, True),
    ("점검기록", "mes", "equipment", False, True),
    ("고장", "mes", "equipment", False, True),
)

_LINE_RE = re.compile(r"([A-Za-z])라인")


def _norm(text: str) -> str:
    return re.sub(r"[\s,.!?~…:'\"·]+", "", text)


def _line_of(norm: str) -> str | None:
    m = _LINE_RE.search(norm)
    return m.group(1).upper() if m else None


def route(text: str) -> Route | None:
    """질의 → 조회군. **모르면 `None`** 이고 그 질문은 여기서 끝이 아니다."""
    norm = _norm(text)
    line = _line_of(norm)
    for keyword, group, endpoint, needs_line, attach_line in RULES:
        if keyword in norm and (not needs_line or line):
            return Route(group, endpoint, {"line": line} if attach_line and line else {})
    return None


# ── 결정론적 답변 템플릿 ──────────────────────────────────────────────


def _euro(word: str) -> str:
    """조사 «으로/로» — 마지막 글자 받침으로 고른다.

    ⚠️ **ㄹ 받침은 «로» 다** (종성 8). 받침 유무만 보면 「한국정밀으로」가
    나온다 — TTS 가 그대로 읽는다.
    """
    last = ord(word[-1]) - 0xAC00 if word else 0
    jong = last % 28 if 0 <= last < 11172 else 0
    return "으로" if jong and jong != 8 else "로"


def _ko_date(iso: Any) -> str:
    """`2026-09-18` → `9월 18일` — TTS 가 자연스럽게 읽는 형태."""
    try:
        _y, m, d = str(iso)[:10].split("-")
        return f"{int(m)}월 {int(d)}일"
    except ValueError:
        return str(iso)


def _source_tail(row: Mapping[str, Any], dropped: int) -> str:
    """«데모 MES 의 13시 42분 자료입니다» + 제외 건수 (`4.7.17` DoD ②)."""
    try:
        hh, mm = str(row["updated_at"])[11:16].split(":")
        tag = f"{row.get('source', 'MES')}의 {int(hh)}시 {int(mm)}분 자료입니다."
    except (KeyError, IndexError, ValueError):
        tag = ""
    note = f"오래된 자료 {dropped}건은 답변에서 제외했습니다." if dropped else ""
    return " ".join(x for x in (tag, note) if x)


def _fresh_only(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    fresh = [r for r in rows if r.get("fresh")]
    return fresh, len(rows) - len(fresh)


def _fmt_production(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "등록된 생산 라인이 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return f"생산 정보의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"
    state_ko = {"running": "가동 중", "stopped": "정지", "idle": "대기"}
    parts = [
        f"{r['line_id']}라인은 목표 {r['target_quantity']:,}개 중"
        f" {r['completed_quantity']:,}개를 완료해 {r['remaining_quantity']:,}개 남았고"
        f" 현재 {state_ko.get(r['state'], r['state'])}입니다"
        for r in rows
    ]
    return ". ".join(parts) + f". {_source_tail(rows[0], dropped)}"


def _fmt_shipments(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "앞으로 7일 이내 출하 예정이 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return f"출하 일정의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"
    parts = [
        f"{_ko_date(r['deadline'])}에 {r['customer']}{_euro(r['customer'])}"
        f" {r['product']} {r['quantity']:,}개"
        for r in rows[:4]
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
        return f"작업지시의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"
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
    if not rows:
        return "등록된 설비 점검 기록이 없습니다."
    rows, dropped = _fresh_only(rows)
    if not rows:
        return f"설비 점검 기록의 마지막 갱신이 허용 시간을 초과했습니다. {UNAVAILABLE}"
    result_ko = {"ok": "정상", "warn": "주의", "fail": "이상"}
    bad = [r for r in rows if r["result"] != "ok"]
    if not bad:
        return f"최근 점검에서 모든 설비가 정상입니다. {_source_tail(rows[0], dropped)}"
    parts = [
        f"{r['equipment']}({r['line_id']}라인 {r['check_item']})"
        f" {result_ko.get(r['result'], r['result'])}"
        for r in bad
    ]
    return (
        f"점검 결과 주의가 필요한 설비가 {len(bad)}곳 있습니다. "
        + ", ".join(parts)
        + f". {CHECK_PROCEDURE} {_source_tail(rows[0], dropped)}"
    )


_FORMAT = {
    "production": _fmt_production,
    "shipments": _fmt_shipments,
    "schedule": _fmt_schedule,
    "equipment": _fmt_equipment,
}


def answer(text: str, providers: Mapping[str, Provider]) -> str | None:
    """운영정보 질의면 답 문장, 아니면 `None` (= 이 경로가 아니다).

    ⚠️ **조회가 실패해도 `None` 이 아니다.** 「자료 없음」과 「이 모듈 소관이
    아님」을 같은 값으로 돌리면, 원본이 죽은 날 질문이 통째로 LLM 에 흘러가
    **그럴듯한 숫자가 나온다.** 실패는 문장으로 말한다.
    """
    hit = route(text)
    if hit is None:
        return None
    provider = providers.get(hit.group)
    if provider is None:
        return UNAVAILABLE
    try:
        rows = provider.query(hit.endpoint, hit.params)
    except UnknownLineError as exc:
        valid = ", ".join(exc.valid)
        return (
            f"그 라인은 등록되어 있지 않습니다. 등록된 라인은 {valid}입니다."
            if valid
            else "등록된 라인이 없습니다."
        )
    except SourceError:
        return UNAVAILABLE
    return _FORMAT[hit.endpoint](rows)
