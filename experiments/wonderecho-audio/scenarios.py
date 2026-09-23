"""Rule-based scenario table (WBS 4.7.7) — PC owns all judgement and lines.

Each scenario is a function taking a `ctx` object:

    ctx.say(text)            synth + play a line through the module speaker
    ctx.listen(timeout)      capture + transcribe one turn -> str or "" if silent
    ctx.retrieve(query)      knowledge snippets for grounding announcements
    ctx.command(name)        run a whitelisted robot action -> result dict
    ctx.event(role, text)    write to the transcript/hub

Spoken lines come from phrases.py — the PC-side voice-response library built
from real industrial manuals (산업안전보건법, KOSHA 지게차 수칙, 화재 대피
매뉴얼, 사업장 출입통제 절차). Every scenario is a deterministic rule — the
voice path has no LLM (ADR-31, ADR-38). Adding or editing a scenario never
touches module firmware.
"""

from __future__ import annotations

import re
from pathlib import Path

import voice_store
from phrases import pick

KNOW_DIR = Path(__file__).with_name("knowledge")
ROSTER_PATH = KNOW_DIR / "직원명단.txt"


def _file_roster():
    """직원명단.txt 한 줄 = 사원 한 명. '#''은 주석."""
    if not ROSTER_PATH.is_file():
        return []
    return [
        ln.strip()
        for ln in ROSTER_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def load_roster():
    """voice_data.db의 roster가 있으면 그쪽이 정본, 없으면 파일 명단."""
    return list(voice_store.roster(_file_roster()))


def _norm(s):
    return re.sub(r"[\s,.!?~…:'\"·]+", "", s)


def _retrieve_first(ctx, query, limit=150):
    """지식 베이스 첫 문단을 짧게 잘라 안내 멘트로 쓴다."""
    body = ctx.retrieve(query)
    return body[:limit] if body else ""


# ══════════════════════════════════════════════════════════════════════════
# 신원·보안 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_guard(ctx):
    """경비모드: 신원 질의 → 명단 규칙 대조 → 판정 멘트. LLM 미사용."""
    ctx.say(pick("identity_ask"))
    answer = ctx.listen(12)
    if not answer:
        ctx.say(pick("identity_retry"))
        answer = ctx.listen(12)
    if not answer:
        ctx.say("응답이 없습니다. 관제 센터에 기록합니다.")
        ctx.event("system", "경비모드: 무응답 → 미확인 기록")
        return
    roster = load_roster()
    claim = _norm(answer)
    hit = next(
        (
            name
            for name in roster
            if re.fullmatch(
                rf"(?:저는|제이름은|사원)?{re.escape(name)}(?:입니다|이에요|라고합니다)?", claim
            )
        ),
        None,
    )
    if hit:
        ctx.say(f"{hit} 님, {pick('identity_ok')}")
        ctx.event("system", f"경비모드: {hit} 확인됨")
    else:
        ctx.say(pick("identity_fail"))
        ctx.event("system", f"경비모드: '{answer}' 미등록 → 방문자 안내")


def sc_visitor_check(ctx):
    """방문자 확인: 방문증 소지 여부 질의 → 규칙 안내."""
    ctx.say(pick("visitor_welcome"))
    answer = ctx.listen(10)
    if "방문증" in _norm(answer) or "받았" in _norm(answer) or "있" in _norm(answer):
        ctx.say(pick("visitor_rules"))
        ctx.event("system", "방문자 확인: 방문증 소지 → 규정 안내")
    else:
        ctx.say(
            "방문증이 없으시면 안내 데스크에서 먼저 발급받아 주세요. 지정 구역 외 출입은 제한됩니다."
        )
        ctx.event("system", "방문자 확인: 방문증 미소지 → 안내 데스크 유도")


# ══════════════════════════════════════════════════════════════════════════
# 안전 경고 시나리오 (PPE·구역·장비)
# ══════════════════════════════════════════════════════════════════════════


def sc_ppe_warning(ctx):
    """안전장비 미착용 경고 — 종합."""
    ctx.say(pick("ppe_full"))
    ctx.event("system", "PPE 경고 방송")


def sc_ppe_helmet(ctx):
    ctx.say(pick("ppe_helmet"))
    ctx.event("system", "안전모 미착용 경고")


def sc_ppe_vest(ctx):
    ctx.say(pick("ppe_vest"))
    ctx.event("system", "안전조끼 미착용 경고")


def sc_ppe_glasses(ctx):
    ctx.say(pick("ppe_glasses"))
    ctx.event("system", "보안경 미착용 경고")


def sc_restricted_zone(ctx):
    """출입제한 구역 경고."""
    ctx.say(pick("restricted_zone"))
    ctx.event("system", "제한구역 경고 방송")


def sc_high_voltage(ctx):
    ctx.say(pick("high_voltage"))
    ctx.event("system", "전기 위험 구역 경고")


def sc_chemical_area(ctx):
    ctx.say(pick("chemical_area"))
    ctx.event("system", "화학물질 구역 주의 안내")


def sc_forklift_pass(ctx):
    """지게차 접근 경고."""
    ctx.say(pick("forklift_pass"))
    ctx.event("system", "지게차 통행 경고")


def sc_forklift_load(ctx):
    """하역 작업 경고."""
    ctx.say(pick("forklift_load"))
    ctx.event("system", "하역 작업 경고")


def sc_smoking_warn(ctx):
    ctx.say(pick("smoking_area"))
    ctx.event("system", "금연 구역 경고")


def sc_photo_warn(ctx):
    ctx.say(pick("photo_warn"))
    ctx.event("system", "촬영 제한 경고")


# ══════════════════════════════════════════════════════════════════════════
# 화재·응급 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_fire_evac(ctx):
    """화재 대피 유도 — 3단계 안내."""
    ctx.say(pick("fire_detected"))
    ctx.say(pick("fire_evac"))
    ctx.say(pick("fire_report"))
    ctx.event("system", "화재 대피 유도 방송")


def sc_emergency_response(ctx):
    """비상 접수 → 위치 확인 → 전파."""
    ctx.say(pick("emergency_ack"))
    ctx.say(pick("emergency_where"))
    answer = ctx.listen(12)
    if answer:
        ctx.say(f"{answer} 위치로 접수했습니다. 담당자가 출발합니다.")
        ctx.event("system", f"비상 접수: 위치='{answer}'")
    else:
        ctx.say("위치를 확인하지 못했습니다. 관제 센터에서 현장을 확인 중입니다.")
        ctx.event("system", "비상 접수: 위치 미확인")


# ══════════════════════════════════════════════════════════════════════════
# 순찰·안내 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_patrol_notice(ctx):
    """순찰 시작 안내 방송."""
    ctx.say(pick("patrol_start"))
    ctx.event("system", "순찰 안내 방송")


def sc_patrol_block(ctx):
    """경로 방해 요청."""
    ctx.say(pick("patrol_block"))
    ctx.event("system", "경로 양보 요청")


def sc_patrol_report(ctx):
    """순찰 완료 보고."""
    ctx.say(pick("patrol_report"))
    ctx.event("system", "순찰 완료 보고")


def sc_visitor_guide(ctx):
    """방문자 안내: 목적지 질의 → 지식 베이스 안내."""
    ctx.say("안녕하세요, 메카독입니다. 어디로 가시는지 말씀해 주세요.")
    answer = ctx.listen(12)
    if not answer:
        ctx.say("안내 데스크는 정문 오른쪽에 있습니다. 도움이 필요하면 다시 불러 주세요.")
        return
    ctx.say("잠시만요, 안내 정보를 확인하겠습니다.")
    body = _retrieve_first(ctx, answer, 120)
    if body:
        ctx.say(f"안내드립니다. {body}")
    else:
        ctx.say("해당 장소 정보를 찾지 못했습니다. 안내 데스크로 가시면 도움받으실 수 있습니다.")
    ctx.event("system", f"방문자 안내: '{answer}'")


# ══════════════════════════════════════════════════════════════════════════
# 일정·공지 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_shift_notice(ctx):
    """교대 시간 안내 — 근무시간교대표 문서 기반."""
    body = _retrieve_first(ctx, "교대", 150)
    ctx.say(f"교대 시간을 안내드립니다. {body}" if body else pick("shift_notice"))
    ctx.event("system", "교대 안내 방송")


def sc_meal_notice(ctx):
    body = _retrieve_first(ctx, "식단", 120)
    ctx.say(f"식사 안내입니다. {body}" if body else pick("meal_notice"))
    ctx.event("system", "식사 안내 방송")


def sc_inspection_notice(ctx):
    """설비점검 사전 안내."""
    body = _retrieve_first(ctx, "점검", 150)
    ctx.say(f"설비 점검 일정을 안내드립니다. {body}" if body else pick("inspection_notice"))
    ctx.event("system", "점검 안내 방송")


def sc_delivery_guide(ctx):
    """출하/배송 안내 — 출하스케줄 기반."""
    body = _retrieve_first(ctx, "출하", 150)
    ctx.say(
        f"출하 일정을 안내드립니다. {body}"
        if body
        else pick("delivery_guide") or "출하 일정을 확인해 주세요."
    )
    ctx.event("system", "출하 안내 방송")


def sc_safety_reminder(ctx):
    """안전 수칙 상기 — 안전규정 문서 기반."""
    body = _retrieve_first(ctx, "안전", 150)
    ctx.say(
        f"안전 수칙을 안내드립니다. {body}"
        if body
        else "안전모, 안전조끼, 보안경 착용을 확인해 주세요."
    )
    ctx.event("system", "안전 수칙 방송")


def sc_general_notice(ctx):
    """일반 공지 — 공지사항 문서 기반."""
    body = _retrieve_first(ctx, "공지", 180)
    ctx.say(f"공지사항입니다. {body}" if body else "현재 등록된 공지가 없습니다.")
    ctx.event("system", "공지 방송")


# ══════════════════════════════════════════════════════════════════════════
# 환경·기상 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_weather_rain(ctx):
    ctx.say(pick("weather_rain"))
    ctx.event("system", "우천 주의 방송")


def sc_weather_cold(ctx):
    ctx.say(pick("weather_cold"))
    ctx.event("system", "한파 주의 방송")


def sc_weather_heat(ctx):
    ctx.say(pick("weather_heat"))
    ctx.event("system", "폭염 주의 방송")


# ══════════════════════════════════════════════════════════════════════════
# 야간·특수 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_night_patrol(ctx):
    """야간 순찰 모드 안내."""
    ctx.say(pick("night_patrol"))
    ctx.event("system", "야간 순찰 안내")


def sc_drill_evac(ctx):
    """대피 훈련 — 시작 → 대피 → 종료."""
    ctx.say(pick("drill_start"))
    ctx.say(pick("fire_evac"))
    ctx.say(pick("drill_end"))
    ctx.event("system", "대피 훈련 방송")


# ══════════════════════════════════════════════════════════════════════════
# 상태·정보 시나리오
# ══════════════════════════════════════════════════════════════════════════


def sc_safety_check(ctx):
    """일일 안전점검 안내 — 체크리스트 문서 기반."""
    body = _retrieve_first(ctx, "점검", 180)
    ctx.say(
        f"안전 점검 항목을 안내드립니다. {body}"
        if body
        else "보호구 착용과 통로 확보를 확인해 주세요."
    )
    ctx.event("system", "안전점검 안내")


def sc_lost_found(ctx):
    """분실물 안내."""
    ctx.say(pick("lost_found"))
    ctx.event("system", "분실물 안내 방송")


def sc_who_are_you(ctx):
    """자기소개."""
    ctx.say(pick("smalltalk_who"))
    ctx.event("system", "자기소개")


def sc_what_doing(ctx):
    """현재 작업 설명."""
    ctx.say(pick("smalltalk_what"))
    ctx.event("system", "현재 작업 안내")


# ══════════════════════════════════════════════════════════════════════════
# 레지스트리
# ══════════════════════════════════════════════════════════════════════════

SCENARIOS = {
    # 신원·보안
    "guard": ("경비모드 신원 확인", sc_guard),
    "visitor_check": ("방문자 방문증 확인", sc_visitor_check),
    # 안전 경고
    "ppe_warning": ("안전장비 종합 경고", sc_ppe_warning),
    "ppe_helmet": ("안전모 미착용 경고", sc_ppe_helmet),
    "ppe_vest": ("안전조끼 미착용 경고", sc_ppe_vest),
    "ppe_glasses": ("보안경 미착용 경고", sc_ppe_glasses),
    "restricted_zone": ("출입제한 구역 경고", sc_restricted_zone),
    "high_voltage": ("전기 위험 구역 경고", sc_high_voltage),
    "chemical_area": ("화학물질 구역 주의", sc_chemical_area),
    "forklift_pass": ("지게차 통행 경고", sc_forklift_pass),
    "forklift_load": ("하역 작업 경고", sc_forklift_load),
    "smoking_warn": ("금연 구역 경고", sc_smoking_warn),
    "photo_warn": ("촬영 제한 경고", sc_photo_warn),
    # 화재·응급
    "fire_evac": ("화재 대피 유도", sc_fire_evac),
    "emergency_response": ("비상 접수·위치 확인", sc_emergency_response),
    # 순찰·안내
    "patrol_notice": ("순찰 시작 안내", sc_patrol_notice),
    "patrol_block": ("경로 양보 요청", sc_patrol_block),
    "patrol_report": ("순찰 완료 보고", sc_patrol_report),
    "visitor_guide": ("방문자 목적지 안내", sc_visitor_guide),
    # 일정·공지
    "shift_notice": ("교대 시간 안내", sc_shift_notice),
    "meal_notice": ("식사 시간 안내", sc_meal_notice),
    "inspection_notice": ("설비점검 일정 안내", sc_inspection_notice),
    "delivery_guide": ("출하 일정 안내", sc_delivery_guide),
    "safety_reminder": ("안전 수칙 상기", sc_safety_reminder),
    "general_notice": ("일반 공지 방송", sc_general_notice),
    # 환경·기상
    "weather_rain": ("우천 주의 안내", sc_weather_rain),
    "weather_cold": ("한파 주의 안내", sc_weather_cold),
    "weather_heat": ("폭염 주의 안내", sc_weather_heat),
    # 야간·훈련
    "night_patrol": ("야간 순찰 안내", sc_night_patrol),
    "drill_evac": ("대피 훈련", sc_drill_evac),
    # 상태·정보
    "safety_check": ("일일 안전점검 안내", sc_safety_check),
    "lost_found": ("분실물 안내", sc_lost_found),
    "who_are_you": ("자기소개", sc_who_are_you),
    "what_doing": ("현재 작업 안내", sc_what_doing),
}

# 음성 트리거: 정규화된 발화에 이 구문이 포함되면 시나리오 실행
TRIGGERS = {
    # 신원·보안
    "경비모드": "guard",
    "신원확인": "guard",
    "방문자확인": "visitor_check",
    # 안전 경고
    "경고방송": "ppe_warning",
    "안전장비경고": "ppe_warning",
    "안전모경고": "ppe_helmet",
    "안전조끼경고": "ppe_vest",
    "보안경경고": "ppe_glasses",
    "제한구역": "restricted_zone",
    "전기위험": "high_voltage",
    "화학물질": "chemical_area",
    "지게차경고": "forklift_pass",
    "하역경고": "forklift_load",
    "금연": "smoking_warn",
    "촬영금지": "photo_warn",
    # 화재·응급
    "화재대피": "fire_evac",
    "대피방송": "fire_evac",
    "비상접수": "emergency_response",
    # 순찰
    "순찰안내": "patrol_notice",
    "길막": "patrol_block",
    "비켜": "patrol_block",
    "순찰보고": "patrol_report",
    "방문자안내": "visitor_guide",
    # 일정·공지
    "교대안내": "shift_notice",
    "식사안내": "meal_notice",
    "점심안내": "meal_notice",
    "점검안내": "inspection_notice",
    "출하안내": "delivery_guide",
    "안전수칙": "safety_reminder",
    "공지사항": "general_notice",
    "공지해": "general_notice",
    # 환경
    "비온다": "weather_rain",
    "우천": "weather_rain",
    "춥다": "weather_cold",
    "한파": "weather_cold",
    "덥다": "weather_heat",
    "폭염": "weather_heat",
    # 야간·훈련
    "야간순찰": "night_patrol",
    "대피훈련": "drill_evac",
    "훈련시작": "drill_evac",
    # 상태·정보
    "안전점검": "safety_check",
    "분실물": "lost_found",
    "누구야": "who_are_you",
    "넌누구": "who_are_you",
    "뭐하고있": "what_doing",
    "뭐하는중": "what_doing",
}


def match_trigger(norm_query: str):
    """정규화된 질의에서 시나리오 이름 반환, 없으면 None.

    트리거 표는 voice_data.db의 scenario_triggers가 우선하고, 없으면
    위의 TRIGGERS 코드 기본값이 쓰인다 (voice_store 참조).
    """
    for phrase, name in voice_store.scenario_triggers(TRIGGERS).items():
        if phrase in norm_query:
            return name
    return None
