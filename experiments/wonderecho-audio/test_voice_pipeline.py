"""Scenario/robotlink/transport/phrases unit tests — no serial, mic, or GPU required."""

import types
import unittest
from unittest import mock

import phrases as phr
import robotlink
import scenarios
import voice_pipeline as vp


class FakeCtx:
    """Records scenario side effects without hardware."""

    def __init__(self, answers=(), roster_docs="", status="배터리 8.5볼트"):
        self.lines = []
        self.events = []
        self.answers = list(answers)
        self.docs = roster_docs
        self.status = status
        self.commands = []

    def say(self, text):
        self.lines.append(text)

    def listen(self, _timeout_s=12.0):
        return self.answers.pop(0) if self.answers else ""

    def retrieve(self, _query):
        return self.docs

    def robot_status(self):
        return self.status

    def command(self, action):
        self.commands.append(action)
        return True

    def event(self, role, text):
        self.events.append((role, text))


class ScenarioTriggerTests(unittest.TestCase):
    def test_trigger_phrases_map_to_registry_names(self):
        for phrase, name in scenarios.TRIGGERS.items():
            self.assertIn(name, scenarios.SCENARIOS)
            self.assertEqual(scenarios.match_trigger(f"메카독{phrase}해줘"), name)

    def test_unrelated_query_falls_through(self):
        self.assertIsNone(scenarios.match_trigger("오늘점심뭐야"))
        self.assertIsNone(scenarios.match_trigger(""))

    def test_every_scenario_has_desc_and_callable(self):
        self.assertGreaterEqual(len(scenarios.SCENARIOS), 30)
        for name, (desc, func) in scenarios.SCENARIOS.items():
            self.assertTrue(desc, name)
            self.assertTrue(callable(func), name)

    def test_scenario_categories_cover_factory_situations(self):
        names = set(scenarios.SCENARIOS)
        # 신원·보안
        self.assertIn("guard", names)
        self.assertIn("visitor_check", names)
        # 안전 경고
        self.assertIn("ppe_warning", names)
        self.assertIn("restricted_zone", names)
        self.assertIn("forklift_pass", names)
        # 화재·응급
        self.assertIn("fire_evac", names)
        self.assertIn("emergency_response", names)
        # 순찰·안내·일정
        self.assertIn("patrol_notice", names)
        self.assertIn("visitor_guide", names)
        self.assertIn("shift_notice", names)
        # 기상·야간·훈련·정보
        self.assertIn("night_patrol", names)
        self.assertIn("drill_evac", names)
        self.assertIn("robot_briefing", names)


class GuardScenarioTests(unittest.TestCase):
    def test_known_name_is_verified(self):
        ctx = FakeCtx(answers=["김민수 입니다"])
        scenarios.sc_guard(ctx)
        self.assertTrue(any("확인되었습니다" in line for line in ctx.lines))
        self.assertTrue(any("김민수" in line for line in ctx.lines))

    def test_unknown_name_is_denied(self):
        ctx = FakeCtx(answers=["홍길동 입니다"])
        scenarios.sc_guard(ctx)
        # 거부 문구는 identity_fail 라이브러리 중 하나가 나와야 한다
        self.assertTrue(any(line in phr.PHRASES["identity_fail"] for line in ctx.lines))
        self.assertFalse(any(line in phr.PHRASES["identity_ok"] for line in ctx.lines))

    def test_silence_is_logged_not_verified(self):
        ctx = FakeCtx(answers=[])
        scenarios.sc_guard(ctx)
        self.assertTrue(any("응답이 없습니다" in line for line in ctx.lines))
        self.assertTrue(any(role == "system" for role, _ in ctx.events))


class PhraseLibraryTests(unittest.TestCase):
    def test_all_categories_nonempty(self):
        for cat, lines in phr.PHRASES.items():
            self.assertGreater(len(lines), 0, cat)

    def test_library_is_large(self):
        total = sum(len(lines) for lines in phr.PHRASES.values())
        self.assertGreaterEqual(total, 100)  # 실사 매뉴얼 기반 대량 문구

    def test_phrases_are_spoken_korean(self):
        for cat, text, _custom in phr.all_lines():
            self.assertIsInstance(text, str)
            self.assertTrue(text.strip(), cat)
            # TTS 읽기 적합 — 마크다운·이모지·영어 약어 없음
            self.assertNotIn("*", text)
            self.assertNotIn("```", text)
            self.assertNotRegex(text, r"[\U0001F300-\U0001FAFF]")

    def test_pick_returns_phrase_from_category(self):
        line = phr.pick("ppe_helmet")
        self.assertIn(line, phr.PHRASES["ppe_helmet"])

    def test_pick_unknown_category_is_empty(self):
        self.assertEqual(phr.pick("nonexistent"), "")


class CustomPhraseTests(unittest.TestCase):
    """관리자 추가 문구: JSON 저장·병합·삭제. 기본 문구는 건드리지 않는다."""

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = __import__("pathlib").Path(self.tmp.name) / "custom.json"
        self.patcher_path = mock.patch.object(phr, "CUSTOM_PATH", self.path)
        self.patcher_path.start()
        self.addCleanup(self.patcher_path.stop)
        self.orig = dict(phr.CUSTOM)
        phr.CUSTOM.clear()
        self.addCleanup(phr.CUSTOM.update, self.orig)

    def test_add_persists_and_merges(self):
        cat = phr.add_custom("Greeting", "관리자가 추가한 인사말")
        self.assertEqual(cat, "greeting")
        self.assertIn("관리자가 추가한 인사말", phr.merged()["greeting"])
        saved = phr.load_custom(self.path)
        self.assertEqual(saved["greeting"], ["관리자가 추가한 인사말"])

    def test_new_category_allowed(self):
        phr.add_custom("zone_b3", "B3 구역 안내 문구입니다")
        self.assertIn("zone_b3", phr.merged())

    def test_pick_includes_custom(self):
        phr.CUSTOM["only_custom"] = ["유일한 문구"]
        for _ in range(20):
            self.assertEqual(phr.pick("only_custom"), "유일한 문구")

    def test_remove_only_custom(self):
        phr.add_custom("greeting", "삭제 대상 문구")
        self.assertFalse(phr.remove_custom("greeting", "네, 메카독입니다. 무엇을 도와드릴까요?"))
        self.assertTrue(phr.remove_custom("greeting", "삭제 대상 문구"))
        self.assertNotIn("삭제 대상 문구", phr.merged()["greeting"])

    def test_add_validates_input(self):
        for cat, text in (
            ("", "문구"),
            ("greeting", ""),
            ("카테고리!", "문구"),
            ("greeting", "x" * 201),
        ):
            with self.assertRaises(ValueError):
                phr.add_custom(cat, text)

    def test_duplicate_rejected(self):
        phr.add_custom("greeting", "중복 문구")
        with self.assertRaises(ValueError):
            phr.add_custom("greeting", "중복 문구")
        with self.assertRaises(ValueError):
            phr.add_custom("greeting", phr.PHRASES["greeting"][0])

    def test_load_custom_tolerates_missing_and_corrupt(self):
        self.assertEqual(phr.load_custom(self.path), {})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(phr.load_custom(self.path), {})

    def test_all_lines_marks_custom(self):
        phr.add_custom("greeting", "추가 표시 문구")
        rows = [(c, t, cu) for c, t, cu in phr.all_lines() if t == "추가 표시 문구"]
        self.assertEqual(rows, [("greeting", "추가 표시 문구", True)])


class NewScenarioTests(unittest.TestCase):
    def test_fire_evac_speaks_three_stages(self):
        ctx = FakeCtx()
        scenarios.sc_fire_evac(ctx)
        self.assertGreaterEqual(len(ctx.lines), 3)
        self.assertTrue(any("대피" in line or "비상구" in line for line in ctx.lines))

    def test_emergency_response_asks_location(self):
        ctx = FakeCtx(answers=["3번 창고"])
        scenarios.sc_emergency_response(ctx)
        self.assertTrue(any("위치" in line for line in ctx.lines))
        self.assertTrue(any("3번 창고" in line for line in ctx.lines))

    def test_ppe_scenarios_use_phrase_library(self):
        for scn in (scenarios.sc_ppe_helmet, scenarios.sc_ppe_vest, scenarios.sc_ppe_warning):
            ctx = FakeCtx()
            scn(ctx)
            self.assertTrue(ctx.lines)
            self.assertTrue(any("안전" in line or "보호구" in line for line in ctx.lines))

    def test_visitor_check_guides_without_badge(self):
        ctx = FakeCtx(answers=["아니요 없어요"])
        scenarios.sc_visitor_check(ctx)
        self.assertTrue(any("방문증" in line or "안내 데스크" in line for line in ctx.lines))

    def test_robot_briefing_reports_telemetry(self):
        ctx = FakeCtx(status="배터리 8.5볼트, 동작 상태 IDLE")
        scenarios.sc_robot_briefing(ctx)
        self.assertTrue(any("8.5" in line for line in ctx.lines))

    def test_robot_briefing_honest_when_unreachable(self):
        ctx = FakeCtx(status=None)
        scenarios.sc_robot_briefing(ctx)
        self.assertTrue(any(line in phr.PHRASES["status_fail"] for line in ctx.lines))


class RobotlinkTests(unittest.TestCase):
    def test_whitelist_exact_match_only(self):
        self.assertEqual(robotlink.match_action("메카독비상정지"), None)  # 부분 문자열 불가
        self.assertIsNotNone(robotlink.match_action("비상정지"))
        self.assertIsNotNone(robotlink.match_action("비상 정지!"))  # 구두점 정규화
        self.assertIsNone(robotlink.match_action("앞으로가"))
        self.assertIsNone(robotlink.match_action("정지하고싶어"))

    def test_no_arbitrary_commands(self):
        # 화이트리스트에 없는 동작은 절대 명령으로 변하지 않는다
        for query in ("공격해", "달려", "문열어", "따라와"):
            self.assertIsNone(robotlink.match_action(query))

    def test_answer_query_runs_whitelisted_action(self):
        with mock.patch.object(robotlink, "run_action", return_value=(True, "")) as run:
            handled, spoken = robotlink.answer_query("비상정지")
        self.assertTrue(handled)
        self.assertIn("비상 정지", spoken)
        run.assert_called_once_with("estop", robotlink.DEFAULT_BASE)

    def test_answer_query_status_uses_real_telemetry(self):
        with mock.patch.object(
            robotlink, "fetch_status", return_value="배터리 8.54볼트, 동작 상태 IDLE"
        ):
            handled, spoken = robotlink.answer_query("배터리 어때")
        self.assertTrue(handled)
        self.assertIn("8.54", spoken)

    def test_answer_query_unreachable_robot_is_honest(self):
        with mock.patch.object(robotlink, "fetch_status", return_value=None):
            handled, spoken = robotlink.answer_query("상태 알려줘")
        self.assertTrue(handled)
        self.assertIn("확인할 수 없습니다", spoken)

    def test_answer_query_passes_llm_questions_through(self):
        handled, _ = robotlink.answer_query("회사 복지 제도가 뭐야")
        self.assertFalse(handled)

    def test_run_action_maps_endpoints(self):
        calls = []

        def fake_post(_base, path, body, _timeout=3.0):
            calls.append((path, body))
            return {"accepted": True}

        with mock.patch.object(robotlink, "_post", side_effect=fake_post):
            self.assertEqual(robotlink.run_action("estop")[0], True)
            robotlink.run_action("manual_on")
            robotlink.run_action("manual_off")
            robotlink.run_action("patrol_start")
            robotlink.run_action("patrol_stop")
            self.assertFalse(robotlink.run_action("self_destruct")[0])
        self.assertEqual(
            calls,
            [
                ("/api/command/estop", {}),
                ("/api/command/manual", {"on": True}),
                ("/api/command/manual", {"on": False}),
                ("/api/command/patrol", {"action": "start"}),
                ("/api/command/patrol", {"action": "stop"}),
            ],
        )

    def test_patrol_phrases_whitelisted(self):
        self.assertEqual(robotlink.match_action("순찰 시작"), ("patrol_start", "순찰을 시작합니다"))
        self.assertEqual(robotlink.match_action("순찰 정지"), ("patrol_stop", "순찰을 정지합니다"))
        self.assertIsNone(robotlink.match_action("순찰 열심히 해"))  # 부분 문자열 불가

    def test_run_action_speaks_rejection_detail(self):
        """거절(accepted=False)도 200으로 오므로 본문을 보고 사유를 말한다."""
        with mock.patch.object(
            robotlink,
            "_post",
            return_value={"accepted": False, "detail": "자율 동작 중이 아니다"},
        ):
            ok, spoken = robotlink.run_action("patrol_stop")
        self.assertFalse(ok)
        self.assertEqual(spoken, "자율 동작 중이 아니다")


class HubScenarioQueueTests(unittest.TestCase):
    def test_scenario_item_flows_through_say_queue(self):
        hub = vp.Hub("test")
        hub.enqueue_say("공지입니다")
        hub.enqueue_scenario("guard", urgent=True)
        items = [item for _, _, item in hub.drain_say()]
        self.assertEqual(items[0], ("scenario", "guard"))  # urgent 먼저
        self.assertEqual(items[1], "공지입니다")

    def test_unknown_scenario_rejected_by_handler(self):
        hub = vp.Hub("test")
        handler_cls = vp.make_handler(hub)
        req = types.SimpleNamespace(
            path="/scenario",
            headers={"Content-Length": "0"},
            rfile=__import__("io").BytesIO(b'{"name": "nope"}'),
            wfile=__import__("io").BytesIO(),
            sent=[],
        )

        class FakeHandler(req.__class__):
            pass

        h = handler_cls.__new__(handler_cls)
        h.path, h.headers, h.rfile = req.path, req.headers, req.rfile
        h.wfile, h.request_version = req.wfile, "HTTP/1.1"
        h.send_response = lambda code: req.sent.append(code)
        h.send_header = lambda *_a: None
        h.end_headers = lambda: None
        h.do_POST()
        self.assertEqual(req.sent[0], 404)
        self.assertEqual(hub.say_q.qsize(), 0)
        del FakeHandler


class TransportTests(unittest.TestCase):
    def test_open_transport_requires_port(self):
        with self.assertRaises(ValueError):
            vp.open_transport(types.SimpleNamespace(port=None, baud=0))

    def test_serial_transport_surface(self):
        # 하드웨어 없이 인터페이스 계약만 확인 — 미래 Wi-Fi 구현이 따라야 할 표면
        import transport

        dev = mock.Mock()
        dev.is_open = True
        dev.in_waiting = 7
        dev.write.side_effect = lambda b: len(b)
        dev.read.return_value = b"ab"
        with mock.patch.object(transport, "open_port", return_value=dev):
            link = transport.SerialTransport("COM99", 921600)
        self.assertEqual(link.kind, "serial")
        self.assertEqual(link.write(b"data"), 4)
        self.assertEqual(link.read(2), b"ab")
        self.assertEqual(link.in_waiting, 7)
        link.send_command(0x106)
        dev.write.assert_called()  # command packet framed by stream_client
        link.close()
        dev.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
