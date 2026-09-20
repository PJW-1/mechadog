"""Data preservation, import validation and fail-closed identity regression tests."""

import copy
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import db_transfer as transfer
import factory_mes as mes
import suparest
import voice_store as vs


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.voice = self.root / "voice.db"
        self.mes = self.root / "mes.db"
        vs.seed(self.voice)
        mes.seed(self.mes)
        self.bundle = transfer.export_bundle(self.voice, self.mes)
        self.target = self.root / "merged.db"

    def test_roundtrip_twice_preserves_rows_and_timestamps(self):
        transfer.import_sqlite(self.bundle, self.target)
        second = transfer.import_sqlite(self.bundle, self.target)
        self.assertTrue(Path(second["backup"]).is_file())
        self.assertTrue(all(c["inserted"] == 0 for c in second["tables"].values()))
        exported = transfer.export_bundle(self.target, self.target)
        self.assertEqual(exported["tables"], self.bundle["tables"])

    def test_existing_business_keys_win_and_backup_has_old_values(self):
        transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            conn.execute("UPDATE production_status SET completed_quantity=999 WHERE line_id='A'")
        result = transfer.import_sqlite(self.bundle, self.target)
        for path in (self.target, result["backup"]):
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT completed_quantity FROM production_status WHERE line_id='A'"
                    ).fetchone()[0],
                    999,
                )

    def test_legacy_history_does_not_duplicate_or_overwrite_numeric_ids(self):
        mes.seed(self.target)
        # Legacy rows identical to source: no import_key yet.
        first = transfer.import_sqlite(self.bundle, self.target)
        self.assertEqual(first["tables"]["inspection_log"]["inserted"], 0)
        changed = copy.deepcopy(self.bundle)
        changed["tables"]["inspection_log"][0]["lot"] = "distinct-lot"
        added = transfer.import_sqlite(changed, self.target)
        self.assertEqual(added["tables"]["inspection_log"]["inserted"], 1)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inspection_log").fetchone()[0], 4)

    def test_invalid_last_row_never_creates_target(self):
        self.bundle["tables"]["equipment_check"][-1]["result"] = "invented"
        with self.assertRaises(ValueError):
            transfer.import_sqlite(self.bundle, self.target)
        self.assertFalse(self.target.exists())

    def test_failed_write_rolls_back_earlier_tables(self):
        transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            conn.execute("DELETE FROM keywords")
            conn.execute("DELETE FROM equipment_check")
            conn.execute(
                "CREATE TRIGGER fail_import BEFORE INSERT ON equipment_check BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            transfer.import_sqlite(self.bundle, self.target)
        with closing(sqlite3.connect(self.target)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM keywords").fetchone()[0], 0)

    def test_main_database_tables_rejected(self):
        self.bundle["tables"]["incidents"] = []
        with self.assertRaises(ValueError):
            transfer.validate(self.bundle)

    def test_invalid_action_and_secret_setting_rejected_without_values(self):
        bad = copy.deepcopy(self.bundle)
        bad["tables"]["action_commands"][0]["action"] = "reset_safe"
        with self.assertRaises(ValueError):
            transfer.validate(bad)
        bad = copy.deepcopy(self.bundle)
        bad["tables"]["settings"][0]["key"] = "api_token"
        bad["tables"]["settings"][0]["value"] = "do-not-echo-this"
        with self.assertRaises(ValueError) as exc:
            transfer.validate(bad)
        self.assertNotIn("do-not-echo-this", str(exc.exception))

    def test_counts_dates_boolean_and_duplicates_validated(self):
        mutations = [
            ("production_status", "completed_quantity", -1),
            ("production_status", "updated_at", "2026-09-01T01:00:00"),
            ("factory_rules", "needs_line", "false"),
            ("factory_rules", "endpoint", "../../command/estop"),
            ("shipment_schedule", "deadline", "2026-02-30"),
            ("inspection_log", "defects", 10000),
        ]
        for table, col, value in mutations:
            with self.subTest(table=table, column=col):
                bad = copy.deepcopy(self.bundle)
                bad["tables"][table][0][col] = value
                with self.assertRaises(ValueError):
                    transfer.validate(bad)
        self.bundle["tables"]["keywords"].append(self.bundle["tables"]["keywords"][0].copy())
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
        before = transfer.export_bundle(mes_db=self.mes)["tables"]
        self.assertFalse(mes.seed(self.mes))
        self.assertEqual(transfer.export_bundle(mes_db=self.mes)["tables"], before)

    def test_missing_read_does_not_create_file(self):
        with self.assertRaises(sqlite3.Error):
            transfer.export_bundle(mes_db=self.target)
        self.assertFalse(self.target.exists())
        with self.assertRaises(sqlite3.Error):
            mes.query(mes.Backend(db_path=self.target), "production", {}, 21600)
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


class MesRegressionTest(unittest.TestCase):
    def test_inspection_order_is_time_not_local_id_or_timezone_text(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            mes.seed(path)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("DELETE FROM inspection_log")
                conn.executemany(
                    "INSERT INTO inspection_log(line_id,lot,inspected,defects,result,updated_at) VALUES ('A',?,1,0,'pass',?)",
                    [("new", "2026-09-20T02:00:00+00:00"), ("old", "2026-09-20T10:00:00+09:00")],
                )
            rows = mes.query(mes.Backend(db_path=path), "inspections", {}, 21600)
            self.assertEqual([r["lot"] for r in rows], ["new", "old"])

    def test_naive_invalid_future_dates_fail_stale(self):
        now = datetime.now(mes.KST)
        rows = [
            {"updated_at": x}
            for x in [
                None,
                "bad",
                now.replace(tzinfo=None).isoformat(),
                (now + timedelta(days=1)).isoformat(),
                now.isoformat(),
            ]
        ]
        result = mes._mark_stale(rows, 21600)
        self.assertEqual([r["stale"] for r in result], [True, True, True, True, False])
        self.assertEqual([r["fresh"] for r in result], [False, False, False, False, True])

    def test_latest_equipment_uses_time_not_import_order(self):
        rows = [
            {
                "id": 1,
                "equipment": "press",
                "checked_at": "2026-09-20T12:00:00+09:00",
                "result": "warn",
            },
            {
                "id": 99,
                "equipment": "press",
                "checked_at": "2026-09-19T12:00:00+09:00",
                "result": "ok",
            },
        ]
        self.assertEqual(mes._latest_per_equipment(rows)[0]["result"], "warn")

    def test_cli_backend_overrides_env_default(self):
        with mock.patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://example.invalid",
                "SUPABASE_ANON_KEY": "test",
                "MES_BACKEND": "sqlite",
            },
        ):
            self.assertTrue(mes.Backend.from_env(backend="supabase").remote)


if __name__ == "__main__":
    unittest.main()
