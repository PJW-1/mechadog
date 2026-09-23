"""Robot dashboard link for the voice loop (WBS 4.7.11, 3.8.2, 4.7.12).

Read path: GET {base}/api/telemetry for the FSM state and escalation level the
voice-auth flow needs, and GET {base}/api/events for the journal. Spoken
status reports (battery, distance, ...) were retired on 2026-09-23 (ADR-38):
the target speaker is the robot's MP3 module, which only plays pre-recorded
lines and cannot read out live numbers.

Write path: a **whitelist** of Korean phrases -> existing command endpoints.
Free text can never reach the robot; only exact phrase matches in ACTIONS
trigger a POST, and every action is still subject to the runtime's own safety
gates (estop latching, manual-mode preconditions, ...).
"""

from __future__ import annotations

import json
import re
import urllib.request

import voice_rules

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


def fetch_events(base=DEFAULT_BASE, since=0):
    """GET /api/events?since=N → (events, dropped, latest) 또는 None (연결 실패).

    로봇 측 사건(person_found 등 블랙박스 확정 검출)을 음성 저널로 옮기는 폴링
    경로다 — `/ws/events` 는 브라우저용이라 여기서는 HTTP 커서 폴링을 쓴다.
    """
    try:
        res = _get(base, f"/api/events?since={since}")
    except OSError:
        return None
    events = res.get("events")
    if not isinstance(events, list):
        return None
    return events, res.get("dropped", 0), res.get("latest", since)


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
    "순찰시작": ("patrol_start", "순찰을 시작합니다"),
    "순찰개시": ("patrol_start", "순찰을 시작합니다"),
    "순찰해": ("patrol_start", "순찰을 시작합니다"),
    "순찰정지": ("patrol_stop", "순찰을 정지합니다"),
    "순찰중지": ("patrol_stop", "순찰을 정지합니다"),
    "순찰멈춰": ("patrol_stop", "순찰을 정지합니다"),
}

_ENDPOINTS = {
    "estop": "/api/command/estop",
    "manual_on": "/api/command/manual",
    "manual_off": "/api/command/manual",
    "patrol_start": "/api/command/patrol",
    "patrol_stop": "/api/command/patrol",
}


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


# 명령 어미 — "비상정지해줘"처럼 끝에 붙는 서법·공손 어미만 벗긴다.
# 부정어("하지마")·조건절은 여기 없어 절대 명령으로 번역되지 않는다.
# 긴 어미부터 시도한다 ("로전환해줘"가 "해줘"보다 먼저 떨어져야 한다).
_COMMAND_ENDINGS = sorted(
    (
        "으로전환해주세요",
        "으로전환해줘",
        "로전환해주세요",
        "로전환해줘",
        "으로바꿔주세요",
        "으로바꿔줘",
        "로바꿔주세요",
        "로바꿔줘",
        "해주십시오",
        "해주세요",
        "하십시오",
        "으로전환",
        "로전환해",
        "로바꿔",
        "으로바꿔",
        "로전환",
        "해주길",
        "하세요",
        "해줘요",
        "해주죠",
        "주세요",
        # STT 가 "해줘"를 자주 이렇게 듣는다 — 해져/해죠/하죠/했죠 는 오청 변형.
        "해줘",
        "해져",
        "해죠",
        "하죠",
        "했죠",
        "했어요",
        "했어",
        "시켜",
        "해라",
        "하기",
        "해요",
        "세요",
        "해",
        "줘",
        "요",
    ),
    key=len,
    reverse=True,
)


def match_action(query: str):
    """화이트리스트 액션 — 정규화 후 정확 일치, 또는 명령 어미를 벗긴 일치.

    "비상정지해"·"비상정지해줘" 같은 자연 발화를 받되, 어미 목록에 없는 꼬리
    ("비상정지하지마", "비상정지할까")는 절대 명령이 되지 않는다.
    명령표·어미는 규칙 파일(voice_rules)이 우선하되, estop 구문은
    voice_rules.PROTECTED가 항상 코드 기본값을 되돌린다.
    """
    actions = voice_rules.action_commands(ACTIONS)
    norm = _norm(query)
    if norm in actions:
        return actions[norm]
    # 합성 음성에서 "비상정지해줘"가 "비상정지에"로 인식된 실측 사례만
    # 좁게 허용한다. "에"를 공통 어미로 벗기면 "순찰시작에"도 보행 명령이 된다.
    if norm == "비상정지에":
        return actions["비상정지"]
    stripped = norm
    endings = voice_rules.command_endings(_COMMAND_ENDINGS)
    for _ in range(3):  # "해주세요"처럼 중첩 어미 대비
        for ending in endings:
            if stripped.endswith(ending) and len(stripped) > len(ending):
                stripped = stripped[: -len(ending)]
                break
        else:
            break
    return actions.get(stripped)


def run_action(action: str, base=DEFAULT_BASE):
    """POST the whitelisted command; return (ok, spoken_result)."""
    try:
        if action == "estop":
            res = _post(base, _ENDPOINTS[action], {})
        elif action == "manual_on":
            res = _post(base, _ENDPOINTS[action], {"on": True})
        elif action == "manual_off":
            res = _post(base, _ENDPOINTS[action], {"on": False})
        elif action == "patrol_start":
            res = _post(base, _ENDPOINTS[action], {"action": "start"})
        elif action == "patrol_stop":
            res = _post(base, _ENDPOINTS[action], {"action": "stop"})
        else:
            return False, "지원하지 않는 명령입니다"
    except OSError:
        return False, "로봇 관제 서버에 연결할 수 없습니다"
    # CommandResult.as_dict() 는 accepted 필드를 돌려준다 — 거절(accepted=False)도
    # 200 으로 오므로 본문을 봐야 한다. detail 은 서버가 만든 한국어 사유다.
    if res.get("error") or res.get("accepted") is not True:
        return False, res.get("detail") or "로봇이 명령을 거부했습니다"
    return True, ""


def robot_state_level(base=DEFAULT_BASE):
    """GET /api/telemetry → (FSM 상태, 대응 단계). 연결 실패면 (None, None).

    ⚠️ **상태만으로는 인증 성공과 실패를 가를 수 없다.** `AUTH_WAIT` 를
    나가는 문은 `AUTH_OK`(→ `PATROL`) 와 `AUTH_FAILED`(→ `ALERT`) 둘 다이고,
    가르는 것은 단계다 — 실패는 L3 로 올라간다. 그래서 둘을 같이 읽는다.
    """
    try:
        snap = _get(base, "/api/telemetry")
    except OSError:
        return None, None
    state = snap.get("state")
    esc = snap.get("escalation")
    return (
        state if isinstance(state, str) else None,
        esc if isinstance(esc, str) else None,
    )


def robot_state(base=DEFAULT_BASE):
    """GET /api/telemetry → FSM 상태 문자열("AUTH_WAIT" 등), 또는 None (연결 실패)."""
    return robot_state_level(base)[0]


def post_auth_pending(captured_at_ms, base=DEFAULT_BASE):
    """POST /api/command/auth `{"result": "pending"}` — **말을 받았다**고만 알린다.

    판정이 아니다. 녹음(최대 15초)·무음 1초·전사를 직렬로 하는 동안 로봇의
    `AUTH_WAIT` 30초가 그냥 흐르기 때문에, **말하는 도중에 경보가 되는 것**을
    막으려고 «판정이 오는 중» 을 먼저 알린다 (ADR-37). 런타임이 창을 한 번
    늘려 준다 — 창마다 1회이고 상한이 있다.

    ⚠️ **실패해도 조용히 넘어간다.** 이건 편의이지 안전 장치가 아니다. 여기서
    예외를 올리면 **녹음 루프가 멈춰** 정작 인증 자체가 죽는다.
    """
    payload = {"result": "pending"}
    if captured_at_ms is not None:
        payload["captured_at_ms"] = int(captured_at_ms)
    try:
        res = _post(base, "/api/command/auth", payload)
    except OSError:
        return False, "로봇 관제 서버에 연결할 수 없습니다"
    if res.get("error") or res.get("accepted") is not True:
        return False, res.get("detail") or "로봇이 유예를 받지 않았습니다"
    return True, ""


def post_auth_result(ok, base=DEFAULT_BASE, captured_at_ms=None):
    """POST /api/command/auth — 암구호 **대조 결과만** 보낸다 (WBS 3.8.2).

    인식 텍스트 자체는 이 경로로 보내지 않는다. `AUTH_WAIT` 가 아니면 런타임이
    거절하므로 `accepted=False` 를 그대로 돌려준다 — 그 거절이 상태 가드다.

    `captured_at_ms` 는 **사람이 말한 시각**(epoch ms)이다. 녹음 15초 + 전사에
    수 초가 걸리므로 «말한 시각» 과 «여기 도착한 시각» 이 다르고, 그 사이에
    `AUTH_WAIT` 가 열렸으면 **창 밖의 말이 시도로 세어진다.** 실어 보내면
    런타임이 걸러 준다.
    """
    payload = {"result": "ok" if ok else "fail"}
    if captured_at_ms is not None:
        payload["captured_at_ms"] = int(captured_at_ms)
    try:
        res = _post(base, "/api/command/auth", payload)
    except OSError:
        return False, "로봇 관제 서버에 연결할 수 없습니다"
    if res.get("error") or res.get("accepted") is not True:
        return False, res.get("detail") or "로봇이 인증 결과를 거부했습니다"
    return True, res.get("detail") or ""


def answer_query(query: str, base=DEFAULT_BASE):
    """명령이면 실행 결과 문장을 돌려준다.

    Returns (handled, spoken_text): handled=False -> 명령이 아니다(호출자가 고정 문구로 답한다).
    """
    action = match_action(_norm(query))
    if action:
        name, ack = action
        ok, err = run_action(name, base)
        return True, ack if ok else err
    return False, ""
