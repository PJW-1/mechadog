"""Robot dashboard link for the voice loop (WBS 4.7.10, 4.7.11).

Read path: GET {base}/api/telemetry -> DashboardState.snapshot() — real
telemetry (batt_v, state, escalation) feeds spoken status reports; the LLM
never invents values.

Write path: a **whitelist** of Korean phrases -> existing command endpoints.
Free-form LLM text can never reach the robot; only exact phrase matches in
ACTIONS trigger a POST, and every action is still subject to the runtime's
own safety gates (estop latching, manual-mode preconditions, ...).
"""

from __future__ import annotations

import json
import re
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8000"


def _post(base, path, body, timeout=3.0):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read() or b"{}")


def _get(base, path, timeout=3.0):
    with urllib.request.urlopen(base + path, timeout=timeout) as res:
        return json.loads(res.read() or b"{}")


def fetch_status(base=DEFAULT_BASE):
    """GET /api/telemetry → 사람이 읽을 수 있는 실측 요약 문자열, 또는 None."""
    try:
        snap = _get(base, "/api/telemetry")
    except OSError:
        return None
    tele = snap.get("telemetry") or {}
    esc = snap.get("escalation") or {}
    parts = []
    if tele.get("batt_v") is not None:
        parts.append(f"배터리 {tele['batt_v']:.2f}볼트")
    if tele.get("temp_c") is not None:
        parts.append(f"내부 온도 {tele['temp_c']:.0f}도")
    if snap.get("state"):
        parts.append(f"동작 상태 {snap['state']}")
    if esc.get("level") is not None:
        parts.append(f"대응 단계 {esc['level']}")
    if snap.get("stale"):
        parts.append("링크 지연 상태")
    return ", ".join(parts) if parts else "상태 데이터 없음"


# ── 화이트리스트 명령 ──────────────────────────────────────────────────────
# 정규화된 발화가 패턴과 정확히 일치할 때만 동작한다.
# estop은 음성으로도 즉시 걸 수 있어야 하므로 포함. reset(래치 해제)처럼
# 사람의 현장 확인이 필요한 명령은 의도적으로 제외.

ACTIONS = {
    "비상정지": ("estop", "비상 정지를 실행합니다"),
    "긴급정지": ("estop", "비상 정지를 실행합니다"),
    "스톱": ("estop", "비상 정지를 실행합니다"),
    "수동모드": ("manual_on", "수동 제어로 전환합니다"),
    "수동제어": ("manual_on", "수동 제어로 전환합니다"),
    "자동모드": ("manual_off", "수동 제어를 해제합니다"),
    "수동해제": ("manual_off", "수동 제어를 해제합니다"),
}

_ENDPOINTS = {
    "estop": "/api/command/estop",
    "manual_on": "/api/command/manual",
    "manual_off": "/api/command/manual",
}


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


def match_action(query: str):
    """정규화된 질의와 정확히 일치하는 화이트리스트 액션, 없으면 None."""
    norm = _norm(query)
    return ACTIONS.get(norm)


def run_action(action: str, base=DEFAULT_BASE):
    """POST the whitelisted command; return (ok, spoken_result)."""
    try:
        if action == "estop":
            res = _post(base, _ENDPOINTS[action], {})
        elif action == "manual_on":
            res = _post(base, _ENDPOINTS[action], {"manual": True})
        elif action == "manual_off":
            res = _post(base, _ENDPOINTS[action], {"manual": False})
        else:
            return False, "지원하지 않는 명령입니다"
    except OSError:
        return False, "로봇 관제 서버에 연결할 수 없습니다"
    if res.get("ok") is False or res.get("error"):
        return False, "로봇이 명령을 거부했습니다"
    return True, ""


def answer_query(query: str, base=DEFAULT_BASE):
    """상태 질의면 실측 요약 문자열, 명령이면 실행 결과 문자열, 아니면 None.

    Returns (handled, spoken_text): handled=False -> fall through to the LLM.
    """
    norm = _norm(query)
    action = match_action(norm)
    if action:
        name, ack = action
        ok, err = run_action(name, base)
        return True, ack if ok else err
    if any(w in norm for w in ("배터리", "상태", "온도", "보고", "잔량", "충전")):
        st = fetch_status(base)
        return (
            True,
            f"현재 상태입니다. {st}"
            if st
            else "로봇 관제에 연결되지 않아 상태를 확인할 수 없습니다",
        )
    return False, ""
