"""Rule-based scenario table (WBS 4.7.7) — PC owns all judgement and lines.

Each scenario is a function taking a `ctx` object:

    ctx.say(text)            synth + play a line through the module speaker
    ctx.listen(timeout)      capture + transcribe one turn -> str or "" if silent
    ctx.retrieve(query)      knowledge snippets for grounding announcements
    ctx.robot_status()       latest telemetry dict, or None when unreachable
    ctx.command(name)        run a whitelisted robot action -> result dict
    ctx.event(role, text)    write to the transcript/hub

Scenarios that touch identity or safety are deterministic rules — the LLM is
never in the verdict path (ADR-31: 판정 로직은 규칙 기반). Scenarios live on
the PC; adding or editing one never touches module firmware.
"""

from __future__ import annotations

import re
from pathlib import Path

KNOW_DIR = Path(__file__).with_name("knowledge")
ROSTER_PATH = KNOW_DIR / "직원명단.txt"


def load_roster():
    """직원명단.txt 한 줄 = 사원 한 명. '#''은 주석."""
    if not ROSTER_PATH.is_file():
        return []
    return [
        ln.strip()
        for ln in ROSTER_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


# ── 시나리오 정의 ──────────────────────────────────────────────────────────


def sc_guard(ctx):
    """경비모드: 신원 질의 → 명단 규칙 대조 → 판정 멘트. LLM 미사용."""
    ctx.say("경비 모드를 시작합니다. 사원증을 제시하거나 성명을 말씀해 주세요.")
    answer = ctx.listen(12)
    if not answer:
        ctx.say("응답이 없습니다. 관제 센터에 기록합니다.")
        ctx.event("system", "경비모드: 무응답 → 미확인 기록")
        return
    roster = load_roster()
    hit = next((name for name in roster if name in _norm(answer)), None)
    if hit:
        ctx.say(f"{hit} 님, 신원이 확인되었습니다. 안전한 작업 되십시오.")
        ctx.event("system", f"경비모드: {hit} 확인됨")
    else:
        ctx.say("신원이 확인되지 않았습니다. 안내 데스크에서 방문증을 받아 주세요.")
        ctx.event("system", f"경비모드: '{answer}' 미등록 → 방문자 안내")


def sc_ppe_warning(ctx):
    """안전장비 미착용 경고 — 감지기 연동 없이도 관제에서 호출 가능."""
    ctx.say(
        "안전 경고입니다. 해당 구역에서는 안전모와 안전조끼 착용이 필수입니다. 확인 후 착용해 주세요."
    )
    ctx.event("system", "PPE 경고 방송")


def sc_patrol_notice(ctx):
    """순찰 시작 안내 방송."""
    ctx.say("메카독이 이 구역 순찰을 시작합니다. 작업자 여러분은 이동 경로에 주의해 주세요.")
    ctx.event("system", "순찰 안내 방송")


def sc_visitor_guide(ctx):
    """방문자 안내: 목적지 질의 → 지식 베이스 안내."""
    ctx.say("안녕하세요, 메카독입니다. 어디로 가시는지 말씀해 주세요.")
    answer = ctx.listen(12)
    if not answer:
        ctx.say("안내 데스크는 정문 오른쪽에 있습니다. 도움이 필요하면 다시 불러 주세요.")
        return
    ctx.say("잠시만요, 안내 정보를 확인하겠습니다.")
    docs = ctx.retrieve(answer)
    if docs:
        # 지식 베이스 문서를 짧게 요약해서 읽어준다 (LLM 없이 첫 문장 사용)
        first = docs.splitlines()[0] if docs.splitlines() else docs
        ctx.say(f"안내드립니다. {first[:120]}")
    else:
        ctx.say("해당 장소 정보를 찾지 못했습니다. 안내 데스크로 가시면 도움받으실 수 있습니다.")
    ctx.event("system", f"방문자 안내: '{answer}'")


def sc_shift_notice(ctx):
    """교대 시간 안내 — 근무시간교대표 문서 기반."""
    body = ctx.retrieve("교대")
    if body:
        ctx.say(f"교대 시간을 안내드립니다. {body[:150]}")
    else:
        ctx.say("교대표 정보를 불러오지 못했습니다.")
    ctx.event("system", "교대 안내 방송")


def sc_safety_reminder(ctx):
    """안전 수칙 상기 — 안전규정 문서 기반."""
    body = ctx.retrieve("안전")
    if body:
        ctx.say(f"안전 수칙을 안내드립니다. {body[:150]}")
    else:
        ctx.say("안전모, 안전조끼, 보안경 착용을 확인해 주세요.")
    ctx.event("system", "안전 수칙 방송")


def sc_emergency_drill(ctx):
    """비상 대피 훈련 안내."""
    ctx.say(
        "지금부터 대피 훈련을 안내합니다. 비상구는 각 층 양 끝 녹색 유도등 방향입니다. 엘리베이터는 사용하지 마세요."
    )
    ctx.event("system", "대피 훈련 방송")


def sc_delivery_guide(ctx):
    """출하/배송 안내 — 출하스케줄 기반."""
    body = ctx.retrieve("출하")
    if body:
        ctx.say(f"출하 일정을 안내드립니다. {body[:150]}")
    else:
        ctx.say("출하 스케줄 정보를 불러오지 못했습니다.")
    ctx.event("system", "출하 안내 방송")


def sc_restricted_zone(ctx):
    """출입제한 구역 경고."""
    ctx.say(
        "경고, 이 구역은 출입 제한 구역입니다. 허가 없는 출입은 기록됩니다. 지정 경로로 이동해 주세요."
    )
    ctx.event("system", "제한구역 경고 방송")


def sc_night_patrol(ctx):
    """야간 순찰 모드 안내."""
    ctx.say("야간 순찰을 시작합니다. 잔류 작업자는 관제 센터에 위치를 보고해 주세요.")
    ctx.event("system", "야간 순찰 안내")


def sc_weather_advisory(ctx):
    """우천·악천후 주의 방송."""
    ctx.say("기상 주의 안내입니다. 실외 구역 바닥이 미끄러울 수 있습니다. 이동 시 주의해 주세요.")
    ctx.event("system", "기상 주의 방송")


def sc_inspection_notice(ctx):
    """설비점검 사전 안내."""
    body = ctx.retrieve("점검")
    if body:
        ctx.say(f"설비 점검 일정을 안내드립니다. {body[:150]}")
    else:
        ctx.say("점검 일정 정보를 불러오지 못했습니다.")
    ctx.event("system", "점검 안내 방송")


def sc_lost_found(ctx):
    """분실물 안내."""
    ctx.say(
        "분실물은 안내 데스크에서 접수 및 수령하실 수 있습니다. 습득물이 있으면 가까운 직원에게 전달해 주세요."
    )
    ctx.event("system", "분실물 안내 방송")


def sc_robot_briefing(ctx):
    """로봇 자기 상태 브리핑 — 텔레메트리 실측 기반."""
    st = ctx.robot_status()
    if not st:
        ctx.say("로봇 관제 연결이 없어 현재 상태를 확인할 수 없습니다.")
        return
    ctx.say(f"현재 상태 보고입니다. {st}")
    ctx.event("system", "상태 브리핑")


# 시나리오 레지스트리: 이름 → (설명, 함수)
SCENARIOS = {
    "guard": ("경비모드 신원 확인", sc_guard),
    "ppe_warning": ("안전장비 미착용 경고", sc_ppe_warning),
    "patrol_notice": ("순찰 시작 안내", sc_patrol_notice),
    "visitor_guide": ("방문자 목적지 안내", sc_visitor_guide),
    "shift_notice": ("교대 시간 안내", sc_shift_notice),
    "safety_reminder": ("안전 수칙 상기", sc_safety_reminder),
    "emergency_drill": ("대피 훈련 안내", sc_emergency_drill),
    "delivery_guide": ("출하 일정 안내", sc_delivery_guide),
    "restricted_zone": ("출입제한 구역 경고", sc_restricted_zone),
    "night_patrol": ("야간 순찰 안내", sc_night_patrol),
    "weather_advisory": ("기상 주의 안내", sc_weather_advisory),
    "inspection_notice": ("설비점검 일정 안내", sc_inspection_notice),
    "lost_found": ("분실물 안내", sc_lost_found),
    "robot_briefing": ("로봇 상태 브리핑", sc_robot_briefing),
}

# 음성 트리거: 정규화된 발화에 이 구문이 포함되면 시나리오 실행 (LLM 우회)
TRIGGERS = {
    "경비모드": "guard",
    "신원확인": "guard",
    "경고방송": "ppe_warning",
    "안전장비경고": "ppe_warning",
    "순찰안내": "patrol_notice",
    "방문자안내": "visitor_guide",
    "교대안내": "shift_notice",
    "안전수칙": "safety_reminder",
    "대피훈련": "emergency_drill",
    "출하안내": "delivery_guide",
    "제한구역": "restricted_zone",
    "야간순찰": "night_patrol",
    "기상안내": "weather_advisory",
    "점검안내": "inspection_notice",
    "분실물": "lost_found",
    "상태보고": "robot_briefing",
}


def match_trigger(norm_query: str):
    """정규화된 질의에서 시나리오 이름 반환, 없으면 None."""
    for phrase, name in TRIGGERS.items():
        if phrase in norm_query:
            return name
    return None
