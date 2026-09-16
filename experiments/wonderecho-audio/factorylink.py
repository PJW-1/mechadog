"""Factory data link for the voice loop — virtual MES read path (auxiliary).

robotlink.py 와 같은 원칙: 숫자·상태·최신성 판정은 코드가 하고 LLM은
개입하지 않는다. 답변은 결정론적 템플릿으로 만들고 출처·갱신시각을 붙인다.
실제 MES가 생기면 DEFAULT_BASE만 실 API로 바꾼다 — 이 파일은 그대로다.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import voice_store

DEFAULT_BASE = "http://127.0.0.1:8095"


def _get(base, path, params=None, timeout=3.0):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 4xx/5xx에도 오류 JSON이 붙어 있다 — 본문을 살려 "연결 불가"와 구분한다.
        try:
            return json.loads(e.read() or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"error": f"http {e.code}"}


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


_LINE_RE = re.compile(r"([A-Za-z])라인")


def _line_of(norm):
    m = _LINE_RE.search(norm)
    return m.group(1).upper() if m else None


# 키워드 → MES 엔드포인트 규칙 기본값(정본).
# (keyword, endpoint, needs_line, attach_line, priority)
#   needs_line : 라인 표기("A라인")가 있을 때만 적용 — "지금 상태 어때"를 잡지 않게
#   attach_line: 라인 표기가 있으면 params에 담는다
#   priority   : 작을수록 먼저 평가 — 구체적 키워드가 넓은 키워드보다 앞서야 한다
DEFAULT_RULES = [
    ("생산량", "production", 0, 1, 10),
    ("생산현황", "production", 0, 1, 10),
    ("진행률", "production", 0, 1, 10),
    ("몇개만들", "production", 0, 1, 10),
    ("생산목표", "production", 0, 1, 10),
    ("목표대비", "production", 0, 1, 10),
    ("가동", "production", 1, 1, 20),
    ("정지", "production", 1, 1, 20),
    ("상태", "production", 1, 1, 20),
    ("돌아가", "production", 1, 1, 20),
    ("멈춰", "production", 1, 1, 20),
    ("세워", "production", 1, 1, 20),
    ("라인상태", "production", 0, 0, 30),
    ("라인현황", "production", 0, 0, 30),
    ("가동현황", "production", 0, 0, 30),
    ("가동중인라인", "production", 0, 0, 30),
    ("어느라인", "production", 0, 0, 30),
    ("출하", "shipments", 0, 0, 40),
    ("납품", "shipments", 0, 0, 40),
    ("납기", "shipments", 0, 0, 40),
    ("배송일정", "shipments", 0, 0, 40),
    ("출하일정", "shipments", 0, 0, 40),
    ("작업지시", "schedule", 0, 1, 50),
    ("지시서", "schedule", 0, 1, 50),
    ("작업순서", "schedule", 0, 1, 50),
    ("우선순위", "schedule", 0, 1, 50),
    ("작업스케줄", "schedule", 0, 1, 50),
    ("불량", "inspections", 0, 1, 60),
    ("검사결과", "inspections", 0, 1, 60),
    ("품질검사", "inspections", 0, 1, 60),
    ("수율", "inspections", 0, 1, 60),
    ("검사현황", "inspections", 0, 1, 60),
    ("설비점검", "equipment", 0, 1, 70),
    ("설비상태", "equipment", 0, 1, 70),
    ("점검기록", "equipment", 0, 1, 70),
    ("설비이력", "equipment", 0, 1, 70),
]


def classify(norm):
    """정규화된 질의 → (endpoint, params) 또는 None.

    LLM보다 먼저 잡는 경로라 '공장 데이터로 답할 수 있는 질문'만 잡는다.
    애매하면 None → RAG/LLM으로 넘긴다.
    규칙 테이블은 voice_store 가 DB 행을 우선하고, 없으면 DEFAULT_RULES.
    """
    line = _line_of(norm)
    for kw, endpoint, needs_line, attach_line, _pri in voice_store.factory_rules():
        if kw in norm and (not needs_line or line):
            return endpoint, ({"line": line} if attach_line and line else {})
    return None


def is_factory_query(norm):
    return classify(norm) is not None


# ── 결정론적 답변 템플릿 ──────────────────────────────────────────────────
# 숫자는 코드가 포맷한다 — LLM이 '약 300개'처럼 뭉개는 일을 막는다.


def _src_tag(row):
    """'데모 MES의 13시 42분 자료입니다' 꼬리표."""
    try:
        hhmm = row["updated_at"][11:16]
        hh, mm = hhmm.split(":")
        return f"{row.get('source', 'MES')}의 {int(hh)}시 {int(mm)}분 자료입니다."
    except (KeyError, IndexError, ValueError):
        return ""


def _stale_answer(what):
    return f"{what}의 마지막 갱신이 허용 시간을 초과했습니다. 최신 정보를 확인할 수 없습니다."


def _split_fresh(rows):
    """(신선한 행, 오래돼 제외된 수). 전부 오래됐으면 fresh는 빈 리스트."""
    fresh = [r for r in rows if not r.get("stale")]
    return fresh, len(rows) - len(fresh)


def _tail(row, n_stale):
    """출처 꼬리표 + 제외 건수 안내."""
    note = f"오래된 자료 {n_stale}건은 답변에서 제외했습니다." if n_stale else ""
    return " ".join(x for x in (_src_tag(row), note) if x)


def _fmt_production(rows):
    if not rows:
        return "등록된 생산 라인이 없습니다."
    rows, n_stale = _split_fresh(rows)
    if not rows:
        return _stale_answer("생산 정보")
    state_ko = {"running": "가동 중", "stopped": "정지", "idle": "대기"}
    parts = []
    for r in rows:
        parts.append(
            f"{r['line_id']}라인은 목표 {r['target_quantity']:,}개 중"
            f" {r['completed_quantity']:,}개를 완료해"
            f" {r['remaining_quantity']:,}개 남았고"
            f" 현재 {state_ko.get(r['state'], r['state'])}입니다"
        )
    return ". ".join(parts) + f". {_tail(rows[0], n_stale)}"


def _euro(word):
    """조사 '으로/로' — 마지막 글자 받침 여부로 고른다."""
    last = ord(word[-1]) - 0xAC00 if word else 0
    return "으로" if 0 <= last < 11172 and last % 28 else "로"


def _ko_date(iso):
    """'2026-09-18' → '9월 18일' — TTS가 자연스럽게 읽는 형태."""
    try:
        _y, m, d = str(iso)[:10].split("-")
        return f"{int(m)}월 {int(d)}일"
    except ValueError:
        return iso


def _fmt_shipments(rows):
    if not rows:
        return "앞으로 7일 이내 출하 예정이 없습니다."
    rows, n_stale = _split_fresh(rows)
    if not rows:
        return _stale_answer("출하 일정")
    parts = [
        f"{_ko_date(r['deadline'])}에 {r['customer']}{_euro(r['customer'])}"
        f" {r['product']} {r['quantity']:,}개"
        for r in rows[:4]
    ]
    n = len(rows)
    head = ", ".join(parts)
    if n > 4:
        head += f" 외 {n - 4}건"
    return f"예정된 출하가 {n}건 있습니다. {head}. {_tail(rows[0], n_stale)}"


def _fmt_schedule(rows):
    if not rows:
        return "진행 중이거나 대기 중인 작업지시가 없습니다."
    rows, n_stale = _split_fresh(rows)
    if not rows:
        return _stale_answer("작업지시")
    prio = rows[0]
    others = len(rows) - 1
    status_ko = {"in_progress": "진행 중", "pending": "대기"}
    s = (
        f"가장 급한 작업은 {prio['line_id']}라인 '{prio['description']}'이고"
        f" {status_ko.get(prio['status'], prio['status'])}입니다"
    )
    if others:
        s += f". 나머지 작업지시가 {others}건 있습니다"
    return s + f". {_tail(prio, n_stale)}"


def _fmt_inspections(rows):
    if not rows:
        return "등록된 검사 기록이 없습니다."
    rows, n_stale = _split_fresh(rows)
    if not rows:
        return _stale_answer("검사 기록")
    result_ko = {"pass": "합격", "fail": "불합격", "hold": "보류"}
    total_i = sum(r["inspected"] for r in rows)
    total_d = sum(r["defects"] for r in rows)
    worst = max(rows, key=lambda r: r["defects"])
    s = f"최근 검사 {len(rows)}건에서 검사 {total_i:,}개 중 불량 {total_d:,}개입니다"
    if total_d:
        s += (
            f". 가장 불량이 많은 건 {worst['line_id']}라인 {worst['lot']}로"
            f" {worst['defects']:,}개이며 판정은"
            f" {result_ko.get(worst['result'], worst['result'])}입니다"
        )
    return s + f". {_tail(rows[0], n_stale)}"


def _fmt_equipment(rows):
    if not rows:
        return "등록된 설비 점검 기록이 없습니다."
    rows, n_stale = _split_fresh(rows)
    if not rows:
        return _stale_answer("설비 점검 기록")
    result_ko = {"ok": "정상", "warn": "주의", "fail": "이상"}
    bad = [r for r in rows if r["result"] != "ok"]
    if not bad:
        return f"최근 점검에서 모든 설비가 정상입니다. {_tail(rows[0], n_stale)}"
    parts = [
        f"{r['equipment']}({r['line_id']}라인 {r['check_item']})"
        f" {result_ko.get(r['result'], r['result'])}"
        for r in bad
    ]
    return (
        f"점검 결과 주의가 필요한 설비가 {len(bad)}곳 있습니다. "
        + ", ".join(parts)
        + f". {_tail(rows[0], n_stale)}"
    )


_FMT = {
    "production": _fmt_production,
    "shipments": _fmt_shipments,
    "schedule": _fmt_schedule,
    "inspections": _fmt_inspections,
    "equipment": _fmt_equipment,
}


def answer_query(query, base=DEFAULT_BASE):
    """공장 데이터 질의 → (handled, spoken_text).

    handled=False 는 이 모듈이 모르는 질문 — LLM으로 넘긴다.
    """
    norm = _norm(query)
    intent = classify(norm)
    if intent is None:
        return False, ""
    endpoint, params = intent
    try:
        res = _get(base, f"/api/{endpoint}", params)
    except OSError:
        return True, "공장 데이터 서버에 연결할 수 없습니다"
    if res.get("error") == "unknown_line":
        valid = ", ".join(res.get("valid_lines") or [])
        return True, (
            f"그 라인은 등록되어 있지 않습니다. 등록된 라인은 {valid}입니다."
            if valid
            else "등록된 라인이 없습니다."
        )
    if res.get("error"):
        return True, "공장 데이터 서버에서 오류가 반환됐습니다"
    return True, _FMT[endpoint](res.get("data") or [])
