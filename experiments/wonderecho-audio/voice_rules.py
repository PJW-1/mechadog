"""Fixed routing rules: code defaults plus one validated local JSON file, no DB/HTTP.

The optional file is voice_data.rules.json next to this module. Each section
replaces its default section. Missing files use the existing code constants.
    python voice_rules.py --rules          # show effective rules
    python voice_rules.py --add wake 메카봇 # edit the local rule file
    python voice_rules.py --del wake 메카봇
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
import warnings
from functools import lru_cache
from pathlib import Path

KINDS = ("wake", "sleep", "resume", "emergency")
SECTIONS = ("keywords", "command_endings", "action_commands", "scenario_triggers")
# 2026-09-23 폐기(ADR-38): 가상 MES 조회(factory_rules), 로봇 상태 음성 응답(status),
# LLM 답변 고지(machine), 상태 브리핑 시나리오. 이전 규칙 파일에 남아 있어도 읽을 때
# 걸러 낸다 — 섹션 하나 때문에 파일 전체가 무효가 되어 사용자 규칙이 조용히 기본값으로
# 돌아가면 안 된다.
RETIRED_SECTIONS = ("factory_rules",)
RETIRED_KINDS = ("status", "machine")
RETIRED_SCENARIOS = ("robot_briefing",)
# 비상정지 구문 — 규칙 파일이 제거·재매핑할 수 없는 최소 안전 집합.
PROTECTED = ("비상정지", "긴급정지", "스톱")
# 음성 DB 를 없앤 뒤에도(2026-09-23) 이름을 그대로 둔다. 바꾸면 PC 에 이미 있는
# 규칙 파일이 조용히 무시되어 사용자 호출어가 기본값으로 돌아간다.
RULES_PATH = Path(__file__).with_name("voice_data.rules.json")
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


def load(path=None):
    """Cache by file identity; invalid edits use defaults and warn once per change."""
    path = Path(path or RULES_PATH).resolve()
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


def words(kind, default, path=None):
    return tuple(load(path)["keywords"].get(kind, default))


def command_endings(default, path=None):
    return sorted(load(path).get("command_endings", default), key=len, reverse=True)


def action_commands(default, path=None):
    data = load(path).get("action_commands", default)
    base = {p: tuple(v) for p, v in data.items()}
    protected = defaults()["action_commands"]
    for phrase in PROTECTED:
        base[phrase] = tuple(protected[phrase])
    return base


def scenario_triggers(default, path=None):
    return load(path).get("scenario_triggers", default)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", type=Path, default=RULES_PATH)
    ap.add_argument(
        "--add", nargs=2, metavar=("KIND", "WORD"), help=f"호출어 추가 ({'|'.join(KINDS)})"
    )
    ap.add_argument("--del", dest="remove", nargs=2, metavar=("KIND", "WORD"), help="호출어 삭제")
    ap.add_argument("--rules", action="store_true", help="유효한 규칙 JSON 출력")
    args = ap.parse_args()
    if args.add or args.remove:
        data = read(args.path) if args.path.exists() else defaults()
        for operation, pair in (("add", args.add), ("remove", args.remove)):
            if not pair:
                continue
            kind, word = pair
            if kind not in KINDS:
                ap.error(f"kind must be one of {KINDS}")
            values = data["keywords"][kind]
            if operation == "add" and word not in values:
                values.append(word)
            elif operation == "remove" and word in values:
                values.remove(word)
        save(args.path, data)
        print(f"[rules] {args.path}")
    if args.rules:
        print(json.dumps(load(args.path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
