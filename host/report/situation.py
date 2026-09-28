"""사건에 규칙 템플릿으로 한국어 한 문장을 붙인다 (WBS 4.8.1 · FR-8.5 · FR-6.7).

클라우드도 로컬 LLM 도 부르지 않는다. *(2026-09-23 개정 — 원래 «이미 도는 로컬 LLM 에
넘겨 만든다» 였다. 음성 LLM 이 폐기돼 도는 LLM 이 없다 · ADR-38)* VLM 판독 결과와
변화 목록을 고정 틀에 채워 만드는 **규칙 템플릿**뿐이다.

⚠️ **문장은 관제 스피커로 그대로 읽힌다** (`4.8.2` 의 방송기가 이 문장을 받는다).
그래서 기호·영문 키를 그대로 넣지 않고, 짧고 소리 내어 읽기 쉬운 문장만 만든다.

대상은 `runtime._record_scene` 이 기록하는 사건 중 **변화 확정과 쓰러짐**뿐이다 —
`person_fallen`(쓰러짐 확정) · `zone_changed`(넘어짐·통로 막힘 확정) ·
`zone_notice`(반출 가벼운 경고). 그 밖의 사건(`PPE_*`·`person_found`·`zone_reading`
등)은 아직 확정된 상황 서술이 아니므로 `None` 을 돌려준다.
"""

from __future__ import annotations

from typing import Any

#: VLM 위험 종류 → 문구 (WBS 4.8.0 `ZONE_HAZARDS`). ⚠️ **`fallen_object` 는 곧
#: `collapsed_load`(적재물 무너짐)로 이름이 바뀔 예정이다** — 이름이 바뀌는 순간
#: 이 표 하나만 놓치면 문장이 조용히 일반 문구로 떨어지므로, 바뀌기 전부터 새
#: 이름도 같이 넣어 둔다.
_HAZARD_PHRASES: dict[str, str] = {
    "fallen_object": "적재물이 무너졌습니다",
    "collapsed_load": "적재물이 무너졌습니다",
    "blocked_path": "통로가 막혔습니다",
}
#: 모르는 위험 종류(표에 없는 `kind`)에 쓰는 일반 문구.
_UNKNOWN_HAZARD_PHRASE = "위험이 감지되었습니다"


def _zone_prefix(zone: Any) -> str:
    """구역 이름이 있으면 «OO 구역에서», 없으면 빈 문자열."""
    if isinstance(zone, str) and zone.strip():
        return f"{zone} 구역에서 "
    return ""


def _describe_person_fallen(_judgement: dict[str, Any]) -> str:
    """쓰러짐 확정. `runtime.py` 두 형태(공장·비공장) 모두 `fallen: True` 확정 뒤에만
    부르므로, 어떤 형태인지 가리지 않고 같은 문장을 쓴다.
    """
    return "사람이 쓰러진 것으로 확인되었습니다. 확인이 필요합니다."


def _describe_zone_changed(judgement: dict[str, Any]) -> str:
    """넘어짐·통로 막힘 확정 (`zone_changed`). `changes` 안의 VLM 위험 항목만 본다.

    ⚠️ **반출·반입이 같은 방문에서 섞여 와도** 결론은 `ZONE_CHANGED` 뿐이므로
    (`_leave_zone` 주석) 여기서도 위험 문구만 말하고 반출·반입은 말하지 않는다.
    """
    changes = judgement.get("changes")
    kinds: list[str] = []
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict) and change.get("source") == "vlm":
                kind = change.get("kind")
                if isinstance(kind, str):
                    kinds.append(kind)
    phrases = dict.fromkeys(_HAZARD_PHRASES.get(kind, _UNKNOWN_HAZARD_PHRASE) for kind in kinds)
    # 기호로 잇지 않고 문장을 나눈다 — 스피커로 읽히면 «·» 는 소리가 되지 않거나 엉뚱하게 읽힌다.
    phrase = ". ".join(phrases) if phrases else _UNKNOWN_HAZARD_PHRASE
    return f"{_zone_prefix(judgement.get('zone'))}{phrase}. 확인이 필요합니다."


def _describe_zone_notice(judgement: dict[str, Any]) -> str:
    """반출 가벼운 경고 (`zone_notice`). L3·눈 변화 없이 관제에만 남기는 경고라
    문장도 «확인이 필요합니다» 없이 사실만 짧게 말한다 (`_leave_zone` Z2).
    """
    return f"{_zone_prefix(judgement.get('zone'))}물건이 반출된 것으로 보입니다."


_TEMPLATES: dict[str, Any] = {
    "person_fallen": _describe_person_fallen,
    "zone_changed": _describe_zone_changed,
    "zone_notice": _describe_zone_notice,
}


def describe(event_type: str, judgement: dict[str, Any] | None) -> str | None:
    """대상 사건에 한국어 한 문장을 붙인다. 대상이 아니면 `None`.

    `judgement` 가 비어 있거나 `None` 이어도(합성 시험·형식이 어긋난 실기록) 죽지
    않고 일반 문구로 채운다 — 호출부(`runtime._record_scene`)는 이 함수를 try/except
    로 감싸지만, 감싸는 것과 별개로 여기서부터 흔한 입력에 안전한 편이 낫다.
    """
    template = _TEMPLATES.get(event_type)
    if template is None:
        return None
    return template(judgement or {})
