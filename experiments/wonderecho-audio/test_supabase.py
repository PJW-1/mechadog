"""Supabase 원격 경로 테스트 — 진짜 Supabase 없이 로컬 스텁 PostgREST로.

계약:
- 읽기: {base}/rest/v1/{table}?<postgrest params> → JSON 배열
- eq.<값> 필터·order·limit 최소 흉내 — 실제 PostgREST 문법 그대로 쓴다.
- 원격 실패 시 voice_store는 로컬 sqlite → 코드 기본값으로 내려간다.
"""

import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import suparest
import voice_pipeline as vp
import voice_store as vs


class StubPostgREST(BaseHTTPRequestHandler):
    """/rest/v1/<table> GET·POST·DELETE를 흉내 내는 최소 스텁."""

    rows = {}  # table -> [dict]
    calls = []  # (method, path, query, headers, body)
    auth_ok = True

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        table = u.path.removeprefix("/rest/v1/")
        q = urllib.parse.parse_qs(u.query)
        self.calls.append(("GET", table, u.query, dict(self.headers), None))
        if not self._auth():
            return
        rows = list(self.rows.get(table, []))
        for col, vals in q.items():
            if col in ("select", "order", "limit"):
                continue
            op, _, val = vals[0].partition(".")
            if op == "eq":
                rows = [r for r in rows if str(r.get(col)) == val]
            elif op == "neq":
                rows = [r for r in rows if str(r.get(col)) != val]
            elif op == "lte":
                rows = [r for r in rows if str(r.get(col, "")) <= val]
            elif op == "gte":
                rows = [r for r in rows if str(r.get(col, "")) >= val]
        if "order" in q:
            for spec in reversed(q["order"][0].split(",")):
                col, _, d = spec.partition(".")
                rows.sort(key=lambda r: r.get(col), reverse=(d == "desc"))
        if "limit" in q:
            rows = rows[: int(q["limit"][0])]
        self._json(rows)

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        table = u.path.removeprefix("/rest/v1/")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("POST", table, u.query, dict(self.headers), body))
        if not self._auth():
            return
        for row in body if isinstance(body, list) else [body]:
            # upsert 흉내: phrase/name/key 중 맞는 PK로 기존 행 교체, 없으면 추가
            merged = False
            for i, old in enumerate(self.rows.get(table, [])):
                if (
                    old.get("phrase") == row.get("phrase")
                    and "phrase" in row
                    or old.get("name") == row.get("name")
                    and "name" in row
                    or old.get("key") == row.get("key")
                    and "key" in row
                ):
                    self.rows[table][i] = {**old, **row}
                    merged = True
            if not merged:
                self.rows.setdefault(table, []).append(row)
        self._json([])

    def do_DELETE(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        table = u.path.removeprefix("/rest/v1/")
        q = urllib.parse.parse_qs(u.query)
        self.calls.append(("DELETE", table, u.query, dict(self.headers), None))
        if not self._auth():
            return
        for col, vals in q.items():
            op, _, val = vals[0].partition(".")
            if op == "eq":
                self.rows[table] = [r for r in self.rows.get(table, []) if str(r.get(col)) != val]
        self._json([])

    def _auth(self):
        if self.auth_ok and not self.headers.get("apikey"):
            self._json({"message": "no apikey"}, 401)
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class RemoteBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "v.db")
        vs.seed(cls.db)
        StubPostgREST.rows = {}
        StubPostgREST.calls = []
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), StubPostgREST)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        StubPostgREST.calls.clear()
        vs._remote_cache.clear()
        patches = [
            mock.patch.object(vs, "_REMOTE_URL", self.base),
            mock.patch.object(vs, "_REMOTE_KEY", "test-anon-key"),
            mock.patch.object(vs, "_REMOTE_TTL", 60.0),
            mock.patch.object(vs, "DEFAULT_DB", Path(self.db)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class RemoteReadTest(RemoteBase):
    def test_fixed_rules_never_read_remote(self):
        StubPostgREST.rows = {"keywords": [{"kind": "wake", "word": "remote-only"}]}
        self.assertEqual(vs.words("wake", ()), vp.WAKE_PREFIXES)
        vs.command_endings(())
        vs.scenario_triggers({})
        self.assertEqual(StubPostgREST.calls, [])

    def test_ttl_cache_for_operational_data(self):
        StubPostgREST.rows = {"settings": [{"key": "follow_s", "value": "45"}]}
        vs.setting("follow_s", 20)
        vs.setting("follow_s", 20)
        self.assertEqual(len(StubPostgREST.calls), 1)

    def test_remote_empty_falls_to_local(self):
        StubPostgREST.rows = {"settings": []}
        self.assertEqual(vs.setting("follow_s", 0, float), vp.FOLLOW_S)

    def test_remote_down_falls_to_local(self):
        with mock.patch.object(vs, "_REMOTE_URL", "http://127.0.0.1:9"):
            self.assertEqual(vs.setting("follow_s", 0, float), vp.FOLLOW_S)

    def test_setting_remote_cast(self):
        StubPostgREST.rows = {"settings": [{"key": "follow_s", "value": "45"}]}
        self.assertEqual(vs.setting("follow_s", 0.0, float), 45.0)

    def test_roster_remote(self):
        StubPostgREST.rows = {"roster": [{"name": "홍길동"}, {"name": "김철수"}]}
        self.assertEqual(vs.roster(()), ("김철수", "홍길동"))  # order=name asc

    def test_actions_ignore_legacy_remote_table(self):
        StubPostgREST.rows = {
            "action_commands": [{"phrase": "순찰시작", "action": "manual_on", "ack": "x"}]
        }
        import robotlink

        acts = vs.action_commands(robotlink.ACTIONS)
        self.assertEqual(acts["순찰시작"], robotlink.ACTIONS["순찰시작"])
        self.assertEqual(StubPostgREST.calls, [])
        self.assertEqual(acts["비상정지"], robotlink.ACTIONS["비상정지"])

    def test_apikey_header_sent(self):
        StubPostgREST.calls.clear()
        suparest.get_rows(self.base, "test-anon-key", "keywords")
        hdrs = StubPostgREST.calls[-1][3]
        self.assertEqual(hdrs.get("Apikey"), "test-anon-key")
        self.assertIn("Bearer test-anon-key", hdrs.get("Authorization", ""))


class RemoteWriteTest(RemoteBase):
    def test_upsert_and_delete(self):
        ok = suparest.upsert_rows(self.base, "k", "keywords", {"kind": "wake", "word": "메카봇"})
        self.assertTrue(ok)
        self.assertEqual(StubPostgREST.rows["keywords"][0]["word"], "메카봇")
        ok = suparest.delete_where(
            self.base, "k", "keywords", {"kind": "eq.wake", "word": "eq.메카봇"}
        )
        self.assertTrue(ok)
        self.assertEqual(StubPostgREST.rows["keywords"], [])


if __name__ == "__main__":
    unittest.main()
