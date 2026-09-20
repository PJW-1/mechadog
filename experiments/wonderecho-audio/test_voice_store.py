"""voice_store operational data / JSON rule tests — no serial, mic, GPU, or robot needed.

계약:
- DB가 없으면 모든 로더가 코드 기본값을 돌려준다 (CI·새 클론에서 DB 불필요).
- seed 후에는 DB 행이 기본값 대신 쓰인다.
- PROTECTED_ACTIONS(estop 구문)는 DB가 지우거나 바꿔도 코드 기본값이 합쳐진다.
"""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import factorylink as fl
import robotlink
import scenarios
import voice_pipeline as vp
import voice_rules as rules
import voice_store as vs


class FallbackTest(unittest.TestCase):
    """DB가 없으면 코드 기본값 — 새 클론/CI 경로."""

    def test_words_fallback(self):
        missing = "없는파일.db"
        self.assertEqual(vs.words("wake", vp.WAKE_PREFIXES, missing), vp.WAKE_PREFIXES)
        self.assertEqual(vs.words("sleep", vp.SLEEP_WORDS, missing), vp.SLEEP_WORDS)
        self.assertEqual(vs.words("emergency", vp.EMERGENCY_WORDS, missing), vp.EMERGENCY_WORDS)

    def test_actions_fallback(self):
        self.assertEqual(vs.action_commands(robotlink.ACTIONS, "없음.db"), robotlink.ACTIONS)

    def test_triggers_fallback(self):
        self.assertEqual(vs.scenario_triggers(scenarios.TRIGGERS, "없음.db"), scenarios.TRIGGERS)

    def test_rules_fallback_sorted(self):
        rules = vs.factory_rules("없음.db")
        self.assertEqual(len(rules), len(fl.DEFAULT_RULES))
        self.assertEqual([r[4] for r in rules], sorted(r[4] for r in rules))

    def test_setting_fallback(self):
        self.assertEqual(vs.setting("follow_s", vp.FOLLOW_S, float, "없음.db"), vp.FOLLOW_S)
        self.assertEqual(
            vs.setting("machine_notice", vp._MACHINE_NOTICE, str, "없음.db"),
            vp._MACHINE_NOTICE,
        )


class OverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "v.db"
        vs.seed(self.db)
        patch = mock.patch.object(vs, "DEFAULT_DB", self.db)
        patch.start()
        self.addCleanup(patch.stop)
        self.data = rules.defaults()

    def save(self):
        rules.save(rules.path_for(self.db), self.data)

    def test_seed_has_only_operational_tables(self):
        self.assertEqual(set(vs.dump(self.db)), {"settings", "roster", "phrases"})
        self.assertEqual(vs.words("wake", ()), vp.WAKE_PREFIXES)
        self.assertEqual(vs.action_commands({}), robotlink.ACTIONS)
        self.assertEqual(vs.scenario_triggers({}), scenarios.TRIGGERS)
        self.assertEqual(vs.setting("follow_s", 0.0, float), vp.FOLLOW_S)

    def test_config_override_reaches_classify_without_db_or_http(self):
        self.data["keywords"]["wake"].append("메카독이")
        self.data["factory_rules"].append(["생산뭐야", "production", 0, 1, 15])
        self.save()
        with (
            mock.patch.object(vs, "_connect", side_effect=AssertionError),
            mock.patch.object(vs, "_remote_rows", side_effect=AssertionError),
        ):
            self.assertIn("메카독이", vs.words("wake", ()))
            self.assertEqual(fl.classify("생산뭐야알려줘"), ("production", {}))

    def test_protected_estop_cannot_be_removed_or_remapped(self):
        del self.data["action_commands"]["비상정지"]
        self.data["action_commands"]["순찰시작"] = ["manual_on", "바뀐멘트"]
        self.save()
        acts = vs.action_commands(robotlink.ACTIONS)
        self.assertEqual(acts["비상정지"], robotlink.ACTIONS["비상정지"])
        self.assertEqual(acts["순찰시작"], ("manual_on", "바뀐멘트"))
        self.data["action_commands"]["스톱"] = ["manual_on", "x"]
        with self.assertRaises(ValueError):
            self.save()

    def test_scenario_trigger_override(self):
        self.data["scenario_triggers"]["불났어"] = "fire_evac"
        self.save()
        self.assertEqual(scenarios.match_trigger("불났어빨리"), "fire_evac")

    def test_setting_cast_and_override(self):
        for value, expected in (("45", 45.0), ("abc", 7.0)):
            conn = sqlite3.connect(self.db)
            with conn:
                conn.execute("INSERT OR REPLACE INTO settings VALUES ('follow_s',?, 'x')", (value,))
            conn.close()
            self.assertEqual(vs.setting("follow_s", 7.0, float), expected)

    def test_endings_sorted_longest_first(self):
        endings = vs.command_endings(())
        self.assertEqual([len(e) for e in endings], sorted((len(e) for e in endings), reverse=True))


if __name__ == "__main__":
    unittest.main()
