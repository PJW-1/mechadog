"""Preserve effective routing, operational data and legacy custom responses on upgrade."""

import copy
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import db_transfer
import migrate_voice_db
import phrases
import robotlink
import voice_pipeline
import voice_rules as rules
import voice_store as store

# 2026-09-23 이전 DB 에만 있던 행 — 옮기지 않고 버려야 한다(ADR-38).
RETIRED_KEYWORDS = [("status", "배터리"), ("status", "상태"), ("machine", "기계")]
RETIRED_FACTORY_RULES = [
    ("생산", "production", 0, 1, 10),
    ("설비", "machines", 0, 1, 20),
]


def legacy_rows():
    data = rules.defaults()
    return {
        "keywords": [{"kind": k, "word": w} for k, words in data["keywords"].items() for w in words]
        + [{"kind": k, "word": w} for k, w in RETIRED_KEYWORDS],
        "command_endings": [{"ending": e} for e in data["command_endings"]],
        "action_commands": [
            {"phrase": p, "action": a, "ack": ack}
            for p, (a, ack) in data["action_commands"].items()
        ],
        "scenario_triggers": [
            {"phrase": p, "scenario": s} for p, s in data["scenario_triggers"].items()
        ]
        + [{"phrase": "상태보고", "scenario": "robot_briefing"}],
        "factory_rules": [
            dict(zip(rules.LEGACY["factory_rules"], row, strict=True))
            for row in RETIRED_FACTORY_RULES
        ],
    }


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "voice.db"
        store.seed(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for table, rows in legacy_rows().items():
                cols = rules.LEGACY[table]
                definitions = ",".join(
                    f"{c} {'INTEGER' if c in ('priority', 'needs_line', 'attach_line') else 'TEXT'}"
                    for c in cols
                )
                conn.execute(f"CREATE TABLE {table} ({definitions})")
                conn.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in cols)})",
                    [tuple(r[c] for c in cols) for r in rows],
                )

    def test_preserves_custom_rules_empty_roster_settings_and_backup(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM roster")
            conn.execute("UPDATE settings SET value='45' WHERE key='follow_s'")
            conn.execute("INSERT INTO keywords VALUES ('wake', '맞춤호출')")
            conn.execute("UPDATE action_commands SET action='manual_on' WHERE phrase='스톱'")
        before = store.dump(self.db)
        result = migrate_voice_db.migrate(self.db)
        self.assertTrue(result["changed"])
        self.assertEqual(store.dump(result["backup"]), before)
        after = store.dump(self.db)
        self.assertEqual(set(after), set(store.TABLES))
        for table in store.TABLES:
            self.assertEqual(after[table], before[table])
        self.assertIn("맞춤호출", store.words("wake", (), self.db))
        self.assertEqual(store.action_commands({}, self.db)["스톱"], robotlink.ACTIONS["스톱"])
        self.assertFalse(migrate_voice_db.migrate(self.db)["changed"])

    def test_check_is_read_only(self):
        before = self.db.read_bytes()
        result = migrate_voice_db.migrate(self.db, check=True)
        self.assertEqual(
            sum(result["legacy_rows"].values()),
            sum(len(rows) for rows in legacy_rows().values()),
        )
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(rules.path_for(self.db).exists())
        self.assertFalse(list(self.db.parent.glob("*.bak")))

    def test_invalid_rule_aborts_without_dropping_or_creating_file(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE scenario_triggers SET scenario='unknown' WHERE rowid=1")
        before = store.dump(self.db)
        with self.assertRaises(ValueError):
            migrate_voice_db.migrate(self.db)
        self.assertEqual(store.dump(self.db), before)
        self.assertFalse(rules.path_for(self.db).exists())

    def test_rule_write_failure_keeps_original_database(self):
        before = store.dump(self.db)
        with (
            mock.patch.object(rules, "save", side_effect=OSError("test")),
            self.assertRaises(OSError),
        ):
            migrate_voice_db.migrate(self.db)
        self.assertEqual(store.dump(self.db), before)

    def test_existing_rule_conflict_is_not_overwritten(self):
        data = rules.defaults()
        data["keywords"]["wake"].append("다른규칙")
        rules.save(rules.path_for(self.db), data)
        with self.assertRaises(ValueError):
            migrate_voice_db.migrate(self.db)
        self.assertEqual(rules.read(rules.path_for(self.db)), data)
        self.assertIn("keywords", store.dump(self.db))

    def test_legacy_phrases_migrate_once_and_admin_reads_same_rows(self):
        custom = self.db.with_name("phrases_custom.json")
        custom.write_text(json.dumps({"greeting": ["이전 문구", "이전 문구"]}), encoding="utf-8")
        migrate_voice_db.migrate(self.db)
        self.assertFalse(custom.exists())
        self.assertTrue(custom.with_name(custom.name + ".migrated.bak").exists())
        with (
            mock.patch.object(store, "DEFAULT_DB", self.db),
            mock.patch.object(store, "_REMOTE_URL", ""),
        ):
            self.assertEqual(phrases.merged()["greeting"].count("이전 문구"), 1)
            self.assertIn(("greeting", "이전 문구", True), list(phrases.all_lines()))
            self.assertTrue(phrases.remove_custom("greeting", "이전 문구"))
            migrate_voice_db.migrate(self.db)
            self.assertNotIn("이전 문구", phrases.merged()["greeting"])

    def test_old_bundle_converts_rules_without_recreating_tables(self):
        bundle = {
            "format_version": 1,
            "data_kind": "synthetic-demo",
            "tables": {
                **legacy_rows(),
                **{t: rows for t, rows in store.dump(self.db).items() if t in store.TABLES},
            },
        }
        target = self.db.with_name("import.db")
        db_transfer.import_sqlite(bundle, target)
        self.assertEqual(set(store.dump(target)), set(store.TABLES))
        self.assertEqual(rules.read(rules.path_for(target)), rules.defaults())
        sql = db_transfer.supabase_sql(bundle)
        self.assertNotIn('public."keywords"', sql)
        self.assertIn('public."roster"', sql)
        self.assertEqual(bundle["format_version"], 1)  # input not mutated


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
            db = Path(temp) / "voice.db"
            path = rules.path_for(db)
            data = rules.defaults()
            rules.save(path, data)
            with mock.patch.object(rules, "read", wraps=rules.read) as read:
                rules.load(db)
                rules.load(db)
                self.assertEqual(read.call_count, 1)
                data["keywords"]["wake"].append("새호출어")
                rules.save(path, data)
                self.assertIn("새호출어", rules.load(db)["keywords"]["wake"])
                self.assertEqual(read.call_count, 2)
            path.write_text("broken", encoding="utf-8")
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(rules.load(db), rules.defaults())
            path.unlink()
            self.assertEqual(rules.load(db), rules.defaults())

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


class PhraseApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "voice.db"
        for patch in (
            mock.patch.object(store, "DEFAULT_DB", self.db),
            mock.patch.object(store, "_REMOTE_URL", ""),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), voice_pipeline.make_handler(voice_pipeline.Hub("test"))
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def test_admin_roundtrip_uses_database_and_lists_custom_rows(self):
        row = {"category": "test", "text": "관리자 API 시험 문구"}
        self.assertTrue(self.request("/phrases", row)["added"])
        self.assertEqual(store.all_phrases(self.db), [("test", row["text"])])
        listing = self.request("/phrases")
        self.assertEqual(
            next(r for r in listing if r["category"] == "test")["lines"],
            [{"text": row["text"], "custom": True}],
        )
        self.assertTrue(self.request("/phrases/delete", row)["removed"])
        self.assertEqual(store.all_phrases(self.db), [])
        self.assertFalse(self.db.with_name("phrases_custom.json").exists())

    def test_remote_read_only_delete_returns_error_without_local_write(self):
        with (
            mock.patch.object(store, "remote_enabled", return_value=True),
            mock.patch.object(
                store, "_remote_rows", return_value=[{"category": "test", "phrase": "원격 문구"}]
            ),
            mock.patch.dict(store.os.environ, {"SUPABASE_WRITE_KEY": ""}),
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/phrases/delete", {"category": "test", "text": "원격 문구"})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertFalse(self.db.exists())

    def test_failed_database_write_is_reported_and_preserves_data(self):
        store.seed(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_phrase BEFORE INSERT ON phrases BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/phrases", {"category": "test", "text": "저장실패"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(store.all_phrases(self.db), [])


if __name__ == "__main__":
    unittest.main()
