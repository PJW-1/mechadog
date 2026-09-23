"""Data preservation, import validation and fail-closed identity regression tests."""

import copy
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import db_transfer as transfer
import suparest
import voice_rules
import voice_store as vs


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.voice = self.root / "voice.db"
        vs.seed(self.voice)
        self.bundle = transfer.export_bundle(self.voice)
        self.target = self.root / "merged.db"

    def test_roundtrip_twice_preserves_rows_and_timestamps(self):
        transfer.import_sqlite(self.bundle, self.target)
        second = transfer.import_sqlite(self.bundle, self.target)
        self.assertTrue(Path(second["backup"]).is_file())
        self.assertTrue(all(c["inserted"] == 0 for c in second["tables"].values()))
        exported = transfer.export_bundle(self.target)
        self.assertEqual(exported["tables"], self.bundle["tables"])

    def test_existing_business_keys_win_and_backup_has_old_values(self):
        transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            conn.execute("UPDATE settings SET value='45' WHERE key='follow_s'")
        result = transfer.import_sqlite(self.bundle, self.target)
        for path in (self.target, result["backup"]):
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(
                    conn.execute("SELECT value FROM settings WHERE key='follow_s'").fetchone()[0],
                    "45",
                )

    def test_invalid_last_row_never_creates_target(self):
        self.bundle["tables"]["settings"][-1]["updated_at"] = "2026-09-01T01:00:00"
        with self.assertRaises(ValueError):
            transfer.import_sqlite(self.bundle, self.target)
        self.assertFalse(self.target.exists())

    def test_failed_write_rolls_back_earlier_tables(self):
        self.bundle["tables"]["phrases"].append({"category": "greeting", "phrase": "test"})
        transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            conn.execute("DELETE FROM roster")
            conn.execute("DELETE FROM phrases")
            conn.execute(
                "CREATE TRIGGER fail_import BEFORE INSERT ON phrases BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM roster").fetchone()[0], 0)

    def test_main_database_tables_rejected(self):
        self.bundle["tables"]["incidents"] = []
        with self.assertRaises(ValueError):
            transfer.validate(self.bundle)

    def test_retired_mes_tables_rejected_with_clear_error(self):
        for version in (1, 2):
            with self.subTest(format_version=version):
                bad = copy.deepcopy(self.bundle)
                bad["format_version"] = version
                bad["tables"]["production_status"] = []
                with self.assertRaisesRegex(ValueError, "폐기.*production_status"):
                    transfer.validate(bad)

    def test_legacy_v1_bundle_imports_voice_rules_without_retired_entries(self):
        legacy = {
            "format_version": 1,
            "data_kind": "synthetic-demo",
            "tables": {
                "roster": [{"name": "테스트직원", "note": ""}],
                "keywords": [
                    {"kind": "wake", "word": "옛호출어"},
                    {"kind": "status", "word": "배터리"},
                ],
                "scenario_triggers": [
                    {"phrase": "안전점검", "scenario": "safety_check"},
                    {"phrase": "상태보고", "scenario": "robot_briefing"},
                ],
                "factory_rules": [
                    {
                        "keyword": "생산량",
                        "endpoint": "production",
                        "needs_line": 0,
                        "attach_line": 1,
                        "priority": 10,
                    }
                ],
            },
        }
        bundle = transfer.validate(legacy)
        self.assertEqual(bundle["format_version"], 2)
        self.assertEqual(set(bundle["tables"]), {"roster"})
        self.assertEqual(bundle["rules"]["keywords"]["wake"], ["옛호출어"])
        self.assertNotIn("status", bundle["rules"]["keywords"])
        self.assertNotIn("factory_rules", bundle["rules"])
        self.assertEqual(bundle["rules"]["scenario_triggers"], {"안전점검": "safety_check"})
        result = transfer.import_sqlite(legacy, self.target)
        self.assertEqual(result["rules"], "created")
        saved = voice_rules.read(voice_rules.path_for(self.target))
        self.assertEqual(saved["keywords"]["wake"], ["옛호출어"])

    def test_v2_bundle_with_retired_rule_sections_still_imports(self):
        old = copy.deepcopy(self.bundle)
        old["rules"]["factory_rules"] = [["생산량", "production", 0, 1, 10]]
        old["rules"]["keywords"]["status"] = ["배터리"]
        old["rules"]["scenario_triggers"]["상태보고"] = "robot_briefing"
        self.assertEqual(transfer.validate(old)["rules"], self.bundle["rules"])
        transfer.import_sqlite(old, self.target)
        self.assertEqual(voice_rules.read(voice_rules.path_for(self.target)), self.bundle["rules"])

    def test_invalid_action_and_secret_setting_rejected_without_values(self):
        bad = copy.deepcopy(self.bundle)
        bad["rules"]["action_commands"]["순찰시작"][0] = "reset_safe"
        with self.assertRaises(ValueError):
            transfer.validate(bad)
        bad = copy.deepcopy(self.bundle)
        bad["tables"]["settings"][0]["key"] = "api_token"
        bad["tables"]["settings"][0]["value"] = "do-not-echo-this"
        with self.assertRaises(ValueError) as exc:
            transfer.validate(bad)
        self.assertNotIn("do-not-echo-this", str(exc.exception))

    def test_types_timestamps_and_duplicates_validated(self):
        mutations = [
            ("settings", "updated_at", "2026-09-01T01:00:00"),
            ("settings", "updated_at", "bad"),
            ("roster", "name", ""),
            ("roster", "name", 1),
        ]
        for table, col, value in mutations:
            with self.subTest(table=table, column=col):
                bad = copy.deepcopy(self.bundle)
                bad["tables"][table][0][col] = value
                with self.assertRaises(ValueError):
                    transfer.validate(bad)
        self.bundle["tables"]["roster"].append(self.bundle["tables"]["roster"][0].copy())
        with self.assertRaises(ValueError):
            transfer.validate(self.bundle)

    def test_seed_is_non_destructive_and_reset_backed_up(self):
        with closing(sqlite3.connect(self.voice)) as conn, conn:
            conn.execute("DELETE FROM roster")
            conn.execute("INSERT INTO phrases VALUES ('greeting', 'keep custom phrase')")
        self.assertFalse(vs.seed(self.voice))
        with mock.patch.object(vs, "_REMOTE_URL", ""):
            self.assertEqual(vs.roster(["default"], self.voice), ())
        self.assertTrue(vs.seed(self.voice, reset=True))
        backups = list(self.root.glob("voice.db.*.bak"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM roster").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM phrases").fetchone()[0], 1)

    def test_missing_read_does_not_create_file(self):
        with self.assertRaises(sqlite3.Error):
            transfer.export_bundle(self.target)
        self.assertFalse(self.target.exists())


class IdentityTest(unittest.TestCase):
    def test_malformed_remote_roster_fails_closed(self):
        with (
            mock.patch.object(vs, "remote_enabled", return_value=True),
            mock.patch.object(vs, "_remote_rows", return_value=[{"name": "ok"}, {"name": ""}]),
        ):
            self.assertEqual(vs.roster(["default"]), ())

    def test_corrupt_local_roster_does_not_reactivate_defaults(self):
        with TemporaryDirectory() as tmp, mock.patch.object(vs, "_REMOTE_URL", ""):
            path = Path(tmp) / "bad.db"
            path.write_text("invalid sqlite", encoding="utf-8")
            self.assertEqual(vs.roster(["deleted-user"], path), ())

    def test_remote_empty_or_error_never_uses_local_roster(self):
        for result in (None, []):
            with (
                self.subTest(result=result),
                mock.patch.object(vs, "_REMOTE_URL", "https://example.invalid"),
                mock.patch.object(vs, "_REMOTE_KEY", "test"),
                mock.patch.object(vs, "_remote_cache", {}),
                mock.patch.object(suparest, "get_rows", return_value=result),
            ):
                self.assertEqual(vs.roster(["deleted-user"]), ())

    def test_expired_remote_roster_not_reused_on_outage(self):
        with (
            mock.patch.object(vs, "_REMOTE_URL", "https://example.invalid"),
            mock.patch.object(vs, "_REMOTE_KEY", "test"),
            mock.patch.object(vs, "_REMOTE_TTL", 60),
            mock.patch.object(vs, "_remote_cache", {}),
            mock.patch.object(vs.time, "monotonic", side_effect=[0, 61]),
            mock.patch.object(suparest, "get_rows", side_effect=[[{"name": "revoked-user"}], None]),
        ):
            self.assertEqual(vs.roster([]), ("revoked-user",))
            self.assertEqual(vs.roster(["fallback-user"]), ())


if __name__ == "__main__":
    unittest.main()
