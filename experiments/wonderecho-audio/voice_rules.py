"""Fixed routing rules: code defaults plus one validated local JSON file, no DB/HTTP.

The optional file next to voice_data.db is voice_data.rules.json. Each section
replaces its default section. Missing files use the existing code constants.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import warnings
from functools import lru_cache
from pathlib import Path

from voice_schema import LEGACY  # noqa: F401  (migrate_voice_db·db_transfer 가 여기서 읽는다)

KINDS = ("wake", "sleep", "resume", "emergency")
SECTIONS = ("keywords", "command_endings", "action_commands", "scenario_triggers")
# 2026-09-23 폐기(ADR-38): 가상 MES 조회(factory_rules), 로봇 상태 음성 응답(status),
# LLM 답변 고지(machine), 상태 브리핑 시나리오. 이전 규칙 파일에 남아 있어도 읽을 때
# 걸러 낸다 — 섹션 하나 때문에 파일 전체가 무효가 되어 사용자 규칙이 조용히 기본값으로
# 돌아가면 안 된다.
RETIRED_SECTIONS = ("factory_rules",)
RETIRED_KINDS = ("status", "machine")
RETIRED_SCENARIOS = ("robot_briefing",)
PROTECTED = ("비상정지", "긴급정지", "스톱")
_cache = {}


@lru_cache(maxsize=1)
def _defaults():
    import robotlink
    import scenarios
    import voice_pipeline as vp

    return {
        "format_version": 1,
        "keywords": {
            "wake": list(vp.WAKE_PREFIXES),
            "sleep": list(vp.SLEEP_WORDS),
            "resume": list(vp.RESUME_WORDS),
            "emergency": list(vp.EMERGENCY_WORDS),
        },
        "command_endings": list(robotlink._COMMAND_ENDINGS),
        "action_commands": {p: list(v) for p, v in robotlink.ACTIONS.items()},
        "scenario_triggers": dict(scenarios.TRIGGERS),
    }


def drop_retired(data):
    """폐기된 섹션·호출어 종류·시나리오 트리거를 뺀 사본. 형식 검사는 하지 않는다."""
    if not isinstance(data, dict):
        return data
    data = {k: v for k, v in data.items() if k not in RETIRED_SECTIONS}
    if isinstance(data.get("keywords"), dict):
        data["keywords"] = {k: v for k, v in data["keywords"].items() if k not in RETIRED_KINDS}
    if isinstance(data.get("scenario_triggers"), dict):
        data["scenario_triggers"] = {
            p: s for p, s in data["scenario_triggers"].items() if s not in RETIRED_SCENARIOS
        }
    return data


def defaults():
    return copy.deepcopy(_defaults())


def path_for(db_path):
    return Path(db_path).with_suffix(".rules.json")


def _text(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("규칙에 비어 있지 않은 문자열이 필요합니다")


def validate(data):
    """Reject typos, unknown routes and ambiguous exact action/scenario matches."""
    import robotlink
    import scenarios

    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise ValueError("규칙 format_version은 1이어야 합니다")
    if set(data) != {"format_version", *SECTIONS}:
        raise ValueError("규칙 섹션이 누락되었거나 알 수 없는 섹션이 있습니다")
    if not isinstance(data["keywords"], dict) or set(data["keywords"]) != set(KINDS):
        raise ValueError("호출어 종류가 올바르지 않습니다")
    for values in [*data["keywords"].values(), data["command_endings"]]:
        if not isinstance(values, list):
            raise ValueError("단어 목록이 필요합니다")
        for value in values:
            _text(value)
        if len(values) != len(set(values)):
            raise ValueError("중복 단어가 있습니다")
    for key in ("action_commands", "scenario_triggers"):
        if not isinstance(data[key], dict):
            raise ValueError("명령/시나리오 객체가 필요합니다")
        for phrase in data[key]:
            _text(phrase)
    for phrase, value in data["action_commands"].items():
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("명령에는 action과 확인 문구가 필요합니다")
        action, ack = value
        _text(action)
        _text(ack)
        if action not in robotlink._ENDPOINTS:
            raise ValueError("허용되지 않은 action")
        if phrase in PROTECTED and action != "estop":
            raise ValueError("비상정지 재매핑 금지")
    for scenario in data["scenario_triggers"].values():
        _text(scenario)
        if scenario not in scenarios.SCENARIOS:
            raise ValueError("알 수 없는 scenario")
    if (set(data["action_commands"]) | set(PROTECTED)) & data["scenario_triggers"].keys():
        raise ValueError("명령과 시나리오의 동일 구문 충돌")
    return data


def read(path):
    return validate(drop_retired(json.loads(Path(path).read_text(encoding="utf-8-sig"))))


def load(db_path):
    """Cache by file identity; invalid edits use defaults and warn once per change."""
    path = path_for(db_path).resolve()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except FileNotFoundError:
        return defaults()
    except OSError:
        stamp = None
    hit = _cache.get(path)
    if hit is None or hit[0] != stamp:
        try:
            data = read(path)
        except (OSError, ValueError, TypeError):
            warnings.warn(
                "음성 규칙 파일 오류: 코드 기본 규칙을 사용합니다", RuntimeWarning, stacklevel=2
            )
            data = defaults()
        _cache[path] = (stamp, data)
    return copy.deepcopy(_cache[path][1])


def save(path, data):
    """Validate before atomic replacement; a partial write never becomes live."""
    validate(data)
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def from_legacy(tables):
    """Preserve the effective nonempty DB overlays, including original row order."""
    data = defaults()
    if any(r.get("kind") not in (*KINDS, *RETIRED_KINDS) for r in tables.get("keywords", [])):
        raise ValueError("알 수 없는 keyword kind")
    for kind in KINDS:
        words = [r["word"] for r in tables.get("keywords", []) if r["kind"] == kind]
        if words:
            data["keywords"][kind] = words
    # 구형 DB 의 factory_rules 표는 옮기지 않는다 — 가상 MES 조회는 폐기됐다(ADR-38).
    for table in ("command_endings", "action_commands", "scenario_triggers"):
        rows = tables.get(table, [])
        if not rows:
            continue
        if table == "command_endings":
            data[table] = [r["ending"] for r in rows]
        elif table == "action_commands":
            data[table] = {r["phrase"]: [r["action"], r["ack"]] for r in rows}
        else:
            data[table] = {r["phrase"]: r["scenario"] for r in rows}
    # Old runtime always restored these; migrate the effective behavior, not unsafe rows.
    for phrase in PROTECTED:
        data["action_commands"][phrase] = _defaults()["action_commands"][phrase][:]
    return validate(drop_retired(data))
