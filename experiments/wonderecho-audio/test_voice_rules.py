"""voice_rules JSON rule tests — no serial, mic, GPU, or robot needed.

계약:
- 규칙 파일이 없으면 모든 로더가 코드 기본값을 돌려준다 (CI·새 클론에서 파일 불필요).
- 파일이 있으면 같은 이름의 목록이 기본값 대신 쓰인다.
- PROTECTED(estop 구문)는 파일이 지우거나 바꿔도 코드 기본값이 합쳐진다.
"""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import robotlink
import scenarios
import voice_pipeline as vp
import voice_rules as rules


class FallbackTest(unittest.TestCase):
    """파일이 없으면 코드 기본값 — 새 클론/CI 경로."""

    def test_words_fallback(self):
        missing = "없는파일.rules.json"
        self.assertEqual(rules.words("wake", vp.WAKE_PREFIXES, missing), vp.WAKE_PREFIXES)
        self.assertEqual(rules.words("sleep", vp.SLEEP_WORDS, missing), vp.SLEEP_WORDS)
        self.assertEqual(rules.words("emergency", vp.EMERGENCY_WORDS, missing), vp.EMERGENCY_WORDS)

    def test_actions_fallback(self):
        self.assertEqual(rules.action_commands(robotlink.ACTIONS, "없음.json"), robotlink.ACTIONS)

    def test_triggers_fallback(self):
        self.assertEqual(
            rules.scenario_triggers(scenarios.TRIGGERS, "없음.json"), scenarios.TRIGGERS
        )


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "voice_data.rules.json"
        patch = mock.patch.object(rules, "RULES_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        self.data = rules.defaults()

    def save(self):
        rules.save(self.path, self.data)

    def test_defaults_file_matches_code(self):
        self.save()
        self.assertEqual(rules.words("wake", ()), vp.WAKE_PREFIXES)
        self.assertEqual(rules.action_commands({}), robotlink.ACTIONS)
        self.assertEqual(rules.scenario_triggers({}), scenarios.TRIGGERS)

    def test_config_override_reaches_rules(self):
        self.data["keywords"]["wake"].append("메카독이")
        self.data["scenario_triggers"]["불났어"] = "fire_evac"
        self.save()
        self.assertIn("메카독이", rules.words("wake", ()))
        self.assertEqual(rules.scenario_triggers({})["불났어"], "fire_evac")

    def test_protected_estop_cannot_be_removed_or_remapped(self):
        del self.data["action_commands"]["비상정지"]
        self.data["action_commands"]["순찰시작"] = ["manual_on", "바뀐멘트"]
        self.save()
        acts = rules.action_commands(robotlink.ACTIONS)
        self.assertEqual(acts["비상정지"], robotlink.ACTIONS["비상정지"])
        self.assertEqual(acts["순찰시작"], ("manual_on", "바뀐멘트"))
        self.data["action_commands"]["스톱"] = ["manual_on", "x"]
        with self.assertRaises(ValueError):
            self.save()

    def test_scenario_trigger_override(self):
        self.data["scenario_triggers"]["불났어"] = "fire_evac"
        self.save()
        self.assertEqual(scenarios.match_trigger("불났어빨리"), "fire_evac")

    def test_endings_sorted_longest_first(self):
        endings = rules.command_endings(())
        self.assertEqual([len(e) for e in endings], sorted((len(e) for e in endings), reverse=True))


class RuleValidationTest(unittest.TestCase):
    def test_invalid_routes_and_ambiguous_exact_trigger_rejected(self):
        for section, value in (
            ("action_commands", {"수동": ["unknown", "확인"]}),
            ("scenario_triggers", {"시나리오": "unknown"}),
            ("scenario_triggers", {"순찰시작": "guard"}),
            ("factory_rules", [["생산", "production", 0, 1, 1]]),
        ):
            data = rules.defaults()
            data[section] = value
            with self.subTest(section=section), self.assertRaises(ValueError):
                rules.validate(data)

    def test_retired_entries_in_old_file_are_dropped_not_fatal(self):
        # 폐기 항목 하나 때문에 파일 전체가 무효가 되면 사용자 규칙이 조용히
        # 기본값으로 돌아간다. 읽을 때 걸러 내고 나머지는 살린다(ADR-38).
        with TemporaryDirectory() as temp:
            path = Path(temp) / "voice.rules.json"
            old = rules.defaults()
            old["keywords"]["wake"].append("맞춤호출")
            old["keywords"]["status"] = ["배터리"]
            old["keywords"]["machine"] = ["기계"]
            old["scenario_triggers"]["상태보고"] = "robot_briefing"
            old["factory_rules"] = [["생산", "production", 0, 1, 10]]
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            data = rules.read(path)
        self.assertEqual(set(data), {"format_version", *rules.SECTIONS})
        self.assertEqual(set(data["keywords"]), set(rules.KINDS))
        self.assertIn("맞춤호출", data["keywords"]["wake"])
        self.assertNotIn("상태보고", data["scenario_triggers"])

    def test_cache_observes_atomic_edits_and_invalid_config_falls_back(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "voice.rules.json"
            data = rules.defaults()
            rules.save(path, data)
            with mock.patch.object(rules, "read", wraps=rules.read) as read:
                rules.load(path)
                rules.load(path)
                self.assertEqual(read.call_count, 1)
                data["keywords"]["wake"].append("새호출어")
                rules.save(path, data)
                self.assertIn("새호출어", rules.load(path)["keywords"]["wake"])
                self.assertEqual(read.call_count, 2)
            path.write_text("broken", encoding="utf-8")
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(rules.load(path), rules.defaults())
            path.unlink()
            self.assertEqual(rules.load(path), rules.defaults())

    def test_invalid_edits_never_replace_last_valid_file(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "voice.rules.json"
            rules.save(path, rules.defaults())
            original = path.read_bytes()
            bad = copy.deepcopy(rules.defaults())
            bad["command_endings"] = [""]
            with self.assertRaises(ValueError):
                rules.save(path, bad)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
