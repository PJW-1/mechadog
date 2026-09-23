"""The committed fixture must recreate the real runtime voice store without lost edits."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import db_transfer
import prepare_demo
import voice_rules


class PrepareDemoTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "runtime"
        self.bundle = db_transfer.validate(
            json.loads(prepare_demo.DEFAULT_BUNDLE.read_text(encoding="utf-8"))
        )

    def test_committed_sql_matches_json(self):
        sql = prepare_demo.DEFAULT_BUNDLE.with_name("import_supabase.sql").read_text(
            encoding="utf-8"
        )
        self.assertEqual(sql, db_transfer.supabase_sql(self.bundle))

    def test_fixture_recreates_exact_rows_rules_and_source_dates(self):
        prepare_demo.prepare(self.output)
        voice = self.output / "voice_data.db"
        exported = db_transfer.export_bundle(voice)
        self.assertEqual(exported["tables"], self.bundle["tables"])
        self.assertEqual(exported["rules"], self.bundle["rules"])
        with closing(sqlite3.connect(voice)) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        self.assertEqual(tables, set(db_transfer.VOICE))
        self.assertEqual(
            sorted(f.name for f in self.output.iterdir()),
            ["voice_data.db", "voice_data.rules.json"],
        )

    def test_rerun_preserves_operator_edits_and_existing_rules(self):
        prepare_demo.prepare(self.output)
        with closing(sqlite3.connect(self.output / "voice_data.db")) as conn, conn:
            conn.execute("UPDATE settings SET value='45' WHERE key='follow_s'")
            conn.execute("DELETE FROM roster")
        config = voice_rules.path_for(self.output / "voice_data.db")
        rules = voice_rules.read(config)
        rules["keywords"]["wake"].append("테스트호출")
        voice_rules.save(config, rules)
        result = prepare_demo.prepare(self.output)
        for db in result["databases"].values():
            self.assertEqual(db["status"], "kept existing file")
        with closing(sqlite3.connect(self.output / "voice_data.db")) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM settings WHERE key='follow_s'").fetchone()[0], "45"
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM roster").fetchone()[0], 0)
        self.assertIn("테스트호출", voice_rules.read(config)["keywords"]["wake"])

    def test_invalid_last_row_rejected_before_creating_directory(self):
        self.bundle["tables"]["roster"][-1]["name"] = ""
        path = self.root / "invalid.json"
        path.write_text(json.dumps(self.bundle), encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare_demo.prepare(self.output, path)
        self.assertFalse(self.output.exists())

    def test_legacy_target_requires_migration_and_does_not_create_rules(self):
        self.output.mkdir()
        path = self.output / "voice_data.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE keywords(kind TEXT, word TEXT)")
            conn.execute("INSERT INTO keywords VALUES ('wake','사용자호출어')")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            prepare_demo.prepare(self.output)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(voice_rules.path_for(path).exists())


if __name__ == "__main__":
    unittest.main()
