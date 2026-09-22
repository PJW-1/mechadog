"""factorylink/factory_mes unit tests — no serial, mic, GPU, or robot needed."""

import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import factory_mes
import factorylink as fl
import voice_pipeline as vp


def _serve(db_path, stale_after=21600):
    factory_mes.Handler.src = factory_mes.Backend(db_path=db_path)
    factory_mes.Handler.stale_ttl = dict.fromkeys(factory_mes.STALE_TTL, stale_after)
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
        self.assertTrue(r["fresh"])

    def test_unknown_line_lists_valid(self):
        r = self._get("/api/production?line=Z")
        self.assertEqual(r["error"], "unknown_line")
        self.assertEqual(r["valid_lines"], ["A", "B", "C"])

    def test_per_endpoint_ttl(self):
        # 항목별 TTL — production만 0초면 그것만 stale이고 나머지는 fresh
        src = factory_mes.Backend(db_path=self.db)
        rows = factory_mes.query(src, "production", {}, {"production": 0})
        self.assertTrue(all(r["stale"] and not r["fresh"] for r in rows))
        rows = factory_mes.query(src, "shipments", {}, {"production": 0})
        self.assertTrue(all(r["fresh"] for r in rows))

    def test_scalar_ttl_still_works(self):
        # 정수를 넘기면 전역 TTL로 동작한다 (하위 호환)
        src = factory_mes.Backend(db_path=self.db)
        rows = factory_mes.query(src, "production", {}, 0)
        self.assertTrue(all(r["stale"] for r in rows))

    def test_stale_flag_from_server(self):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE production_status SET updated_at='2020-01-01T00:00:00+09:00' WHERE line_id='A'"
        )
        conn.commit()
        conn.close()
        r = self._get("/api/production?line=A")["data"][0]
        self.assertTrue(r["stale"])
        factory_mes.seed(self.db, reset=True)  # 다른 테스트용 복원

    def test_shipments_bad_days_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/api/shipments?days=abc")
        self.assertEqual(cm.exception.code, 400)

    def test_seed_ids_match_deadlines(self):
        # SH 번호의 MMDD는 deadline과 같은 날짜에서 유도돼야 한다.
        for r in self._get("/api/shipments?days=30")["data"]:
            mmdd = r["deadline"][5:7] + r["deadline"][8:10]
            self.assertIn(mmdd, r["shipment_id"])


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
        # deadline은 ISO 원문이 아니라 TTS용 한국어 날짜로 말한다.
        self.assertIn("월", s)
        self.assertNotIn("-09-", s)

    def test_schedule_priority(self):
        ok, s = fl.answer_query("작업지시 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("가장 급한 작업", s)
        self.assertIn("A라인", s)

    def test_inspections(self):
        ok, s = fl.answer_query("불량 현황 알려줘", self.base)
        self.assertTrue(ok)
        self.assertIn("불량", s)
        self.assertIn("LOT-C", s)
        # 판정은 영어 코드가 아니라 한국어로 말한다.
        self.assertIn("보류", s)
        self.assertNotIn("hold", s)

    def test_unknown_line_answer(self):
        ok, s = fl.answer_query("Z라인 생산량", self.base)
        self.assertTrue(ok)
        self.assertIn("등록되어 있지 않습니다", s)
        self.assertIn("A, B, C", s)

    def test_unknown_line_on_other_endpoints(self):
        # production 외 엔드포인트도 없는 라인과 기록 0건을 구분한다.
        for q in ("Z라인 작업지시", "Z라인 불량", "Z라인 설비점검"):
            ok, s = fl.answer_query(q, self.base)
            self.assertTrue(ok, q)
            self.assertIn("등록되어 있지 않습니다", s, q)
        # 등록된 라인에 기록이 없을 때는 '없다'고 정직하게 말해야 한다.
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM work_schedule WHERE line_id='B'")
        conn.commit()
        conn.close()
        ok, s = fl.answer_query("B라인 작업지시", self.base)
        self.assertTrue(ok)
        self.assertIn("없습니다", s)
        factory_mes.seed(self.db, reset=True)  # 복원

    def test_stale_answer(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE production_status SET updated_at='2020-01-01T00:00:00+09:00'")
        conn.commit()
        conn.close()
        ok, s = fl.answer_query("생산 현황", self.base)
        self.assertTrue(ok)
        self.assertIn("허용 시간을 초과", s)
        factory_mes.seed(self.db, reset=True)

    def test_stale_rows_excluded_not_blocked(self):
        # 일부 행만 오래됐으면 신선한 행은 답하고 제외 사실을 알린다.
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE production_status SET updated_at='2020-01-01T00:00:00+09:00' WHERE line_id='C'"
        )
        conn.commit()
        conn.close()
        ok, s = fl.answer_query("생산 현황", self.base)
        self.assertTrue(ok)
        self.assertIn("A라인", s)
        self.assertNotIn("C라인은", s)  # stale 행은 답변에서 빠진다
        self.assertIn("제외", s)
        factory_mes.seed(self.db, reset=True)

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
