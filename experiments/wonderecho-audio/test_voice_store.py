"""voice_store overlay tests — no serial, mic, GPU, or robot needed.

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
    """DEFAULT_DB를 임시 DB로 패치해 소비자 경로까지 오버레이를 검증한다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "v.db")
        vs.seed(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        # 소비자 코드는 db_path 없이 DEFAULT_DB를 본다 — 테스트는 그것을 패치.
        self._patch = mock.patch.object(vs, "DEFAULT_DB", Path(self.db))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _sql(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def test_seed_mirrors_code_defaults(self):
        self.assertEqual(vs.words("wake", ()), vp.WAKE_PREFIXES)
        self.assertEqual(vs.action_commands({}), robotlink.ACTIONS)
        self.assertEqual(vs.scenario_triggers({}), scenarios.TRIGGERS)
        self.assertEqual(vs.setting("follow_s", 0.0, float), vp.FOLLOW_S)

    def test_keyword_override_reaches_classify(self):
        # "생산뭐야" 같은 운영자 추가 키워드가 classify에 즉시 반영돼야 한다.
        self._sql("INSERT OR IGNORE INTO keywords VALUES ('wake','메카독이')")
        self.assertIn("메카독이", vs.words("wake", ()))
        self._sql("INSERT OR REPLACE INTO factory_rules VALUES ('생산뭐야','production',0,1,15)")
        self.assertEqual(fl.classify("생산뭐야알려줘"), ("production", {}))
        self._sql("DELETE FROM factory_rules WHERE keyword='생산뭐야'")

    def test_protected_estop_cannot_be_removed(self):
        self._sql("DELETE FROM action_commands WHERE phrase='비상정지'")
        self._sql("UPDATE action_commands SET action='manual_on', ack='x' WHERE phrase='스톱'")
        self._sql(
            "UPDATE action_commands SET action='manual_on', ack='바뀐멘트' WHERE phrase='순찰시작'"
        )
        acts = vs.action_commands(robotlink.ACTIONS)
        # 보호 구문은 삭제·재매핑 모두 코드 기본값으로 원복
        self.assertEqual(acts["비상정지"], robotlink.ACTIONS["비상정지"])
        self.assertEqual(acts["스톱"], robotlink.ACTIONS["스톱"])
        # 비보호 구문은 DB 오버레이가 가능
        self.assertEqual(acts["순찰시작"], ("manual_on", "바뀐멘트"))
        vs.seed(self.db)  # 복원

    def test_scenario_trigger_override(self):
        self._sql("INSERT OR REPLACE INTO scenario_triggers VALUES ('불났어','fire_evac')")
        self.assertEqual(scenarios.match_trigger("불났어빨리"), "fire_evac")
        self._sql("DELETE FROM scenario_triggers WHERE phrase='불났어'")

    def test_setting_cast_and_override(self):
        self._sql("INSERT OR REPLACE INTO settings VALUES ('follow_s','45', 'x')")
        self.assertEqual(vs.setting("follow_s", 0.0, float), 45.0)
        self._sql("INSERT OR REPLACE INTO settings VALUES ('follow_s','abc', 'x')")
        self.assertEqual(vs.setting("follow_s", 7.0, float), 7.0)  # 잘못된 값 → 기본값
        vs.seed(self.db)

    def test_endings_sorted_longest_first(self):
        endings = vs.command_endings(())
        self.assertEqual([len(e) for e in endings], sorted((len(e) for e in endings), reverse=True))


if __name__ == "__main__":
    unittest.main()
