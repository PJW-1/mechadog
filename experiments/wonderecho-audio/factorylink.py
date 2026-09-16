"""Factory data link for the voice loop — virtual MES read path (auxiliary).

robotlink.py 와 같은 원칙: 숫자·상태·최신성 판정은 코드가 하고 LLM은
개입하지 않는다. 답변은 결정론적 템플릿으로 만들고 출처·갱신시각을 붙인다.
실제 MES가 생기면 DEFAULT_BASE만 실 API로 바꾼다 — 이 파일은 그대로다.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8095"


def _get(base, path, params=None, timeout=3.0):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read() or b"{}")


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


_LINE_RE = re.compile(r"([A-Za-z])라인")


def _line_of(norm):
    m = _LINE_RE.search(norm)
    return m.group(1).upper() if m else None


def classify(norm):
    """정규화된 질의 → (endpoint, params) 또는 None.

    LLM보다 먼저 잡는 경로라 '공장 데이터로 답할 수 있는 질문'만 잡는다.
    애매하면 None → RAG/LLM으로 넘긴다.
    """
    line = _line_of(norm)
    if any(w in norm for w in ("생산량", "생산현황", "진행률", "몇개만들", "생산목표", "목표대비")):
        return "production", ({"line": line} if line else {})
    if line and any(w in norm for w in ("가동", "정지", "상태", "돌아가", "멈춰", "세워")):
        return "production", {"line": line}
    if any(w in norm for w in ("라인상태", "라인현황", "가동현황", "가동중인라인", "어느라인")):
        return "production", {}
    if any(w in norm for w in ("출하", "납품", "납기", "배송일정", "출하일정")):
        return "shipments", {}
    if any(w in norm for w in ("작업지시", "지시서", "작업순서", "우선순위", "작업스케줄")):
        return "schedule", ({"line": line} if line else {})
    if any(w in norm for w in ("불량", "검사결과", "품질검사", "수율", "검사현황")):
        return "inspections", ({"line": line} if line else {})
    if any(w in norm for w in ("설비점검", "설비상태", "점검기록", "설비이력")):
        return "equipment", ({"line": line} if line else {})
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
    return (
        f"{what}의 마지막 갱신이 허용 시간을 초과했습니다. "
        "최신 정보를 확인할 수 없습니다."
    )


def _fmt_production(rows):
    if not rows:
        return "등록된 생산 라인이 없습니다."
    if any(r.get("stale") for r in rows):
        return _stale_answer("생산 정보")
    state_ko = {"running": "가동 중", "stopped": "정지", "idle": "대기"}
    parts = []
    for r in rows:
        pct = (
            round(r["completed_quantity"] * 100 / r["target_quantity"])
            if r["target_quantity"]
            else 0
        )
        parts.append(
            f"{r['line_id']}라인은 목표 {r['target_quantity']:,}개 중"
            f" {r['completed_quantity']:,}개를 완료해"
            f" {r['remaining_quantity']:,}개 남았고"
            f" 현재 {state_ko.get(r['state'], r['state'])}입니다"
        )
    return ". ".join(parts) + f". {_src_tag(rows[0])}"


def _euro(word):
    """조사 '으로/로' — 마지막 글자 받침 여부로 고른다."""
    last = ord(word[-1]) - 0xAC00 if word else 0
    return "으로" if 0 <= last < 11172 and last % 28 else "로"


def _fmt_shipments(rows):
    if not rows:
        return "앞으로 7일 이내 출하 예정이 없습니다."
    if any(r.get("stale") for r in rows):
        return _stale_answer("출하 일정")
    parts = [
        f"{r['deadline']}에 {r['customer']}{_euro(r['customer'])}"
        f" {r['product']} {r['quantity']:,}개"
        for r in rows[:4]
    ]
    n = len(rows)
    head = ", ".join(parts)
    if n > 4:
        head += f" 외 {n - 4}건"
    return f"예정된 출하가 {n}건 있습니다. {head}. {_src_tag(rows[0])}"


def _fmt_schedule(rows):
    if not rows:
        return "진행 중이거나 대기 중인 작업지시가 없습니다."
    if any(r.get("stale") for r in rows):
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
    return s + f". {_src_tag(prio)}"


def _fmt_inspections(rows):
    if not rows:
        return "등록된 검사 기록이 없습니다."
    if any(r.get("stale") for r in rows):
        return _stale_answer("검사 기록")
    total_i = sum(r["inspected"] for r in rows)
    total_d = sum(r["defects"] for r in rows)
    worst = max(rows, key=lambda r: r["defects"])
    s = f"최근 검사 {len(rows)}건에서 검사 {total_i:,}개 중 불량 {total_d:,}개입니다"
    if total_d:
        s += (
            f". 가장 불량이 많은 건 {worst['line_id']}라인 {worst['lot']}로"
            f" {worst['defects']:,}개이며 판정은 {worst['result']}입니다"
        )
    return s + f". {_src_tag(rows[0])}"


def _fmt_equipment(rows):
    if not rows:
        return "등록된 설비 점검 기록이 없습니다."
    if any(r.get("stale") for r in rows):
        return _stale_answer("설비 점검 기록")
    result_ko = {"ok": "정상", "warn": "주의", "fail": "이상"}
    bad = [r for r in rows if r["result"] != "ok"]
    if not bad:
        return f"최근 점검에서 모든 설비가 정상입니다. {_src_tag(rows[0])}"
    parts = [
        f"{r['equipment']}({r['line_id']}라인 {r['check_item']})"
        f" {result_ko.get(r['result'], r['result'])}"
        for r in bad
    ]
    return (
        f"점검 결과 주의가 필요한 설비가 {len(bad)}곳 있습니다. "
        + ", ".join(parts)
        + f". {_src_tag(rows[0])}"
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
