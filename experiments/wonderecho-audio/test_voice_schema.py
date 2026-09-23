"""All active entry points agree on voice3; legacy layouts require explicit migration."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import db_transfer
import migrate_voice_db
import prepare_demo
import voice_pipeline
import voice_rules
import voice_schema
import voice_store


class SchemaContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "voice_data.db"
        self.bundle = json.loads(prepare_demo.DEFAULT_BUNDLE.read_text(encoding="utf-8"))

    def test_every_creation_path_uses_same_schema_and_version(self):
        for kind in ("seed", "import", "prepare", "edit"):
            with self.subTest(kind=kind):
                path = self.root / kind / "voice_data.db"
                path.parent.mkdir()
                if kind == "seed":
                    voice_store.seed(path)
                elif kind == "import":
                    part = {**self.bundle, "tables": {"roster": []}}
                    db_transfer.import_sqlite(part, path)
                elif kind == "prepare":
                    prepare_demo.prepare(path.parent)
                else:
                    voice_store.edit_phrase("greeting", "테스트 문구", db_path=path)
                status = voice_store.check_schema(path)
                self.assertEqual(status["schema_version"], voice_schema.VERSION)
                self.assertEqual(set(voice_store.dump(path)), set(voice_schema.COLUMNS))

    def test_old_three_table_version_is_backed_up_without_reseeding(self):
        voice_store.seed(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA user_version=0")
            conn.execute("DELETE FROM roster")
            conn.execute("INSERT INTO settings VALUES ('auth_passphrases', '[\"demo-only\"]', 'x')")
        before = self.db.read_bytes()
        migrate_voice_db.migrate(self.db, check=True)
        self.assertEqual(self.db.read_bytes(), before)
        result = migrate_voice_db.migrate(self.db)
        self.assertTrue(result["changed"])
        self.assertEqual(voice_store.check_schema(result["backup"])["schema_version"], 0)
        self.assertEqual(voice_store.check_schema(self.db)["schema_version"], voice_schema.VERSION)
        self.assertFalse(migrate_voice_db.migrate(self.db)["changed"])
        self.assertEqual(voice_store.roster(("fallback",), self.db), ())
        self.assertEqual(
            voice_store.setting("auth_passphrases", "", db_path=self.db), '["demo-only"]'
        )

    def test_legacy_db_cannot_run_seed_edit_or_import_silently(self):
        voice_store.seed(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE keywords(kind TEXT, word TEXT)")
            conn.execute("INSERT INTO keywords VALUES ('wake', '이전호출어')")
        before = self.db.read_bytes()
        for operation in (
            lambda: voice_store.seed(self.db),
            lambda: voice_store.edit_phrase("greeting", "test", db_path=self.db),
            lambda: db_transfer.import_sqlite(self.bundle, self.db),
            lambda: voice_store.check_schema(self.db),
        ):
            with self.assertRaisesRegex(ValueError, "migrate_voice_db"):
                operation()
            self.assertEqual(self.db.read_bytes(), before)
        with (
            mock.patch.object(voice_store, "DEFAULT_DB", self.db),
            mock.patch.object(voice_pipeline, "open_transport") as transport,
            mock.patch.object(voice_store, "_remote_rows") as remote,
            mock.patch("sys.argv", ["voice_pipeline.py", "--say", "test"]),
            self.assertRaises(SystemExit) as stopped,
        ):
            voice_pipeline.main()
        self.assertEqual(stopped.exception.code, 1)
        transport.assert_not_called()
        remote.assert_not_called()
        migrate_voice_db.migrate(self.db)
        voice_store.check_schema(self.db)
        self.assertIn(
            "이전호출어", voice_rules.read(voice_rules.path_for(self.db))["keywords"]["wake"]
        )

    def test_foreign_partial_and_future_schemas_are_never_repaired_implicitly(self):
        for kind in ("foreign", "partial", "future", "bad_pk"):
            with self.subTest(kind=kind):
                path = self.root / f"{kind}.db"
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.executescript(voice_schema.SCHEMA)
                    if kind == "foreign":
                        conn.execute("CREATE TABLE robots(robot_id TEXT)")
                    elif kind == "partial":
                        conn.execute("DROP TABLE roster")
                    elif kind == "future":
                        conn.execute("PRAGMA user_version=999")
                    else:
                        conn.execute("DROP TABLE roster")
                        conn.execute("CREATE TABLE roster(name TEXT, note TEXT)")
                before = path.read_bytes()
                for operation in (voice_store.seed, migrate_voice_db.migrate):
                    with self.assertRaises(ValueError):
                        operation(path)
                    self.assertEqual(path.read_bytes(), before)

    def test_legacy_custom_json_cannot_be_silently_ignored(self):
        voice_store.seed(self.db)
        custom = self.db.with_name("phrases_custom.json")
        custom.write_text('{"greeting": ["preserve me"]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "migrate_voice_db"):
            voice_store.check_schema(self.db)
        migrate_voice_db.migrate(self.db)
        voice_store.check_schema(self.db)
        self.assertIn(("greeting", "preserve me"), voice_store.all_phrases(self.db))


if __name__ == "__main__":
    unittest.main()
