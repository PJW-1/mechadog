"""factorylink/factory_mes unit tests — no serial, mic, GPU, or robot needed."""

import json
import sqlite3
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import factory_mes
import factorylink as fl
import voice_pipeline as vp


def _serve(db_path, stale_after=21600):
    factory_mes.Handler.db_path = db_path
    factory_mes.Handler.stale_after = stale_after
    srv = ThreadingHTTPServer(("127.0.0.1", 0), factory_mes.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class MesApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "t.db")
        factory_mes.seed(cls.db)
        cls.srv, cls.base = _serve(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as r:
            return json.loads(r.read())

    def test_production_contract(self):
        r = self._get("/api/production?line=A")["data"][0]
        self.assertEqual(r["line_id"], "A")
        self.assertEqual(
            r["remaining_quantity"],
            r["target_quantity"] - r["completed_quantity"],
        )
        self.assertEqual(r["source"], "demo-mes")
        self.assertFalse(r["stale"])

    def test_unknown_line_lists_valid(self):
        r = self._get("/api/production?line=Z")
        self.assertEqual(r["error"], "unknown_line")
        self.assertEqual(r["valid_lines"], ["A", "B", "C"])

    def test_stale_flag_from_server(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE production_status SET updated_at='2020-01-01T00:00:00+09:00' WHERE line_id='A'"
        )
        conn.commit()
        conn.close()
        r = self._get("/api/production?line=A")["data"][0]
        self.assertTrue(r["stale"])
        factory_mes.seed(self.db)  # 다른 테스트용 복원


class FactoryLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "t.db")
        factory_mes.seed(cls.db)
        cls.srv, cls.base = _serve(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def test_production_answer_deterministic(self):
        ok, s = fl.answer_query("A라인 생산량 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("1,200", s)
        self.assertIn("780", s)
        self.assertIn("420", s)
        self.assertIn("demo-mes", s)

    def test_all_lines(self):
        ok, s = fl.answer_query("라인 가동 현황", self.base)
        self.assertTrue(ok)
        self.assertIn("A라인", s)
        self.assertIn("C라인", s)
        self.assertIn("정지", s)

    def test_shipments(self):
        ok, s = fl.answer_query("출하 일정 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("출하", s)
        self.assertIn("한국정밀", s)

    def test_schedule_priority(self):
        ok, s = fl.answer_query("작업지시 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("가장 급한 작업", s)
        self.assertIn("A라인", s)

    def test_inspections(self):
        ok, s = fl.answer_query("불량 현황 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("불량", s)
        self.assertIn("LOT-C0917", s)

    def test_unknown_line_answer(self):
        ok, s = fl.answer_query("Z라인 생산량", self.base)
        self.assertTrue(ok)
        self.assertIn("등록되어 있지 않습니다", s)
        self.assertIn("A, B, C", s)

    def test_stale_answer(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE production_status SET updated_at='2020-01-01T00:00:00+09:00'")
        conn.commit()
        conn.close()
        ok, s = fl.answer_query("생산 현황", self.base)
        self.assertTrue(ok)
        self.assertIn("허용 시간을 초과", s)
        factory_mes.seed(self.db)

    def test_api_down(self):
        ok, s = fl.answer_query("생산 현황", "http://127.0.0.1:9")
        self.assertTrue(ok)
        self.assertIn("연결할 수 없습니다", s)

    def test_non_factory_passthrough(self):
        for q in ("배터리 상태", "오늘 날씨 어때", "안녕"):
            ok, _ = fl.answer_query(q, self.base)
            self.assertFalse(ok, q)


class RoutingTest(unittest.TestCase):
    """route_query 우선순위 — factory는 emergency 뒤·status 앞."""

    def test_factory_routes(self):
        for q, want in (
            ("A라인 생산량 알려줘", "factory"),
            ("출하 일정이 어떻게 돼", "factory"),
            ("작업지시 뭐 있어", "factory"),
            ("불량 현황", "factory"),
            ("배터리 상태 알려줘", "status"),
            ("비상정지", "action"),
            ("안녕하세요", "llm"),
        ):
            self.assertEqual(vp.route_query(q), want, q)


class GuardTest(unittest.TestCase):
    def test_machine_guard_appends_notice(self):
        out = vp.machine_guard("컨베이어가 고장 났어", "벨트를 확인하세요.")
        self.assertIn("담당 기술자", out)

    def test_machine_guard_passes_normal(self):
        out = vp.machine_guard("오늘 식단 뭐야", "김치찌개입니다.")
        self.assertEqual(out, "김치찌개입니다.")

    def test_machine_guard_no_duplicate(self):
        ans = "점검하세요. " + vp._MACHINE_NOTICE
        self.assertEqual(vp.machine_guard("기계 고장", ans), ans)


if __name__ == "__main__":
    unittest.main()
