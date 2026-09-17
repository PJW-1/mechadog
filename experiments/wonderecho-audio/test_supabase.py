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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import factory_mes
import factorylink as fl
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
    def test_words_from_remote(self):
        StubPostgREST.rows = {
            "keywords": [{"kind": "wake", "word": "메카봇"}, {"kind": "wake", "word": "메카독2"}]
        }
        self.assertEqual(vs.words("wake", ()), ("메카독2", "메카봇"))  # order=word asc

    def test_ttl_cache(self):
        StubPostgREST.rows = {"keywords": [{"kind": "wake", "word": "x"}]}
        vs.words("wake", ())
        vs.words("wake", ())
        n = sum(1 for c in StubPostgREST.calls if c[1] == "keywords")
        self.assertEqual(n, 1)  # 두 번째 호출은 캐시

    def test_remote_empty_falls_to_local(self):
        StubPostgREST.rows = {"keywords": []}
        # 원격이 비면 로컬 sqlite(시드된 기본값)로 내려간다
        self.assertEqual(vs.words("wake", ()), vp.WAKE_PREFIXES)

    def test_remote_down_falls_to_local(self):
        # 원격이 죽으면 로컬 DB(→ 기본값)으로 내려간다 — 죽은 포트로 지정
        vs._remote_cache.clear()
        with mock.patch.object(vs, "_REMOTE_URL", "http://127.0.0.1:9"):
            self.assertEqual(vs.words("wake", ("폴백",)), vp.WAKE_PREFIXES)

    def test_setting_remote_cast(self):
        StubPostgREST.rows = {"settings": [{"key": "follow_s", "value": "45"}]}
        self.assertEqual(vs.setting("follow_s", 0.0, float), 45.0)

    def test_roster_remote(self):
        StubPostgREST.rows = {"roster": [{"name": "홍길동"}, {"name": "김철수"}]}
        self.assertEqual(vs.roster(()), ("김철수", "홍길동"))  # order=name asc

    def test_action_commands_remote_keeps_protected(self):
        StubPostgREST.rows = {
            "action_commands": [{"phrase": "순찰시작", "action": "manual_on", "ack": "x"}]
        }
        import robotlink

        acts = vs.action_commands(robotlink.ACTIONS)
        self.assertEqual(acts["순찰시작"], ("manual_on", "x"))
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


class MesRemoteTest(unittest.TestCase):
    """factory_mes가 Supabase 백엔드로도 같은 API 계약을 지키는지."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), StubPostgREST)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        now = __import__("datetime").datetime.now(factory_mes.KST).isoformat(timespec="seconds")
        StubPostgREST.rows = {
            "production_status": [
                {
                    "line_id": "A",
                    "product": "MD-100",
                    "target_quantity": 100,
                    "completed_quantity": 40,
                    "state": "running",
                    "updated_at": now,
                },
                {
                    "line_id": "B",
                    "product": "MD-200",
                    "target_quantity": 50,
                    "completed_quantity": 50,
                    "state": "idle",
                    "updated_at": now,
                },
            ],
            "work_schedule": [],
            "inspection_log": [],
            "equipment_check": [],
            "shipment_schedule": [],
        }
        self.src = factory_mes.Backend(url=self.base, key="k")

    def test_remote_production(self):
        rows = factory_mes.query(self.src, "production", {}, 21600)
        self.assertEqual(len(rows), 2)
        a = next(r for r in rows if r["line_id"] == "A")
        self.assertEqual(a["remaining_quantity"], 60)  # 계산열은 코드에서
        self.assertFalse(a["stale"])
        self.assertEqual(a["source"], "demo-mes")

    def test_remote_unknown_line(self):
        res = factory_mes.query(self.src, "production", {"line": ["Z"]}, 21600)
        self.assertEqual(res["error"], "unknown_line")
        self.assertEqual(res["valid_lines"], ["A", "B"])

    def test_remote_end_to_end_via_http(self):
        # MES API(:8095)까지 통째로 — 음성 응답이 실제로 나오는지
        factory_mes.Handler.src = self.src
        factory_mes.Handler.stale_after = 21600
        mes_srv = ThreadingHTTPServer(("127.0.0.1", 0), factory_mes.Handler)
        threading.Thread(target=mes_srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{mes_srv.server_address[1]}"
            ok, s = fl.answer_query("A라인 생산량 알려줘", base)
            self.assertTrue(ok)
            self.assertIn("A라인", s)
            self.assertIn("60", s)
            health = json.loads(urllib.request.urlopen(base + "/api/health").read())
            self.assertEqual(health["backend"], "supabase")
        finally:
            mes_srv.shutdown()

    def test_source_down_raises(self):
        dead = factory_mes.Backend(url="http://127.0.0.1:9", key="k")
        with self.assertRaises(factory_mes.SourceError):
            factory_mes.query(dead, "production", {}, 21600)


if __name__ == "__main__":
    unittest.main()
