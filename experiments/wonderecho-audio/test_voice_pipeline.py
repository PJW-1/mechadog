"""Scenario/robotlink/transport/phrases unit tests — no serial, mic, or GPU required."""

import sys
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
    def test_guard_check_runs_without_llm_and_closes_com_port(self):
        device = mock.Mock()
        piper = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(load=lambda _path: object()))
        whisper = types.SimpleNamespace(WhisperModel=lambda *_a, **_kw: object())
        with (
            mock.patch.object(
                sys, "argv", ["voice_pipeline.py", "--port", "COM9", "--guard-check"]
            ),
            mock.patch.dict(sys.modules, {"piper": piper, "faster_whisper": whisper}),
            mock.patch.object(vp, "open_transport", return_value=device),
            mock.patch.object(scenarios, "sc_guard") as guard,
        ):
            vp.main()
        guard.assert_called_once()
        device.close.assert_called_once()

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

    def test_negated_or_embedded_name_is_not_verified(self):
        for answer in ("김민수 아닙니다", "김민수 친구입니다"):
            ctx = FakeCtx(answers=[answer])
            scenarios.sc_guard(ctx)
            self.assertTrue(any(line in phr.PHRASES["identity_fail"] for line in ctx.lines))

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
    """관리자 추가 문구: DB 저장·병합·삭제. 기본 문구는 건드리지 않는다."""

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import voice_store

        self.path = __import__("pathlib").Path(self.tmp.name) / "voice.db"
        patch = mock.patch.object(voice_store, "DEFAULT_DB", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        remote = mock.patch.object(voice_store, "_REMOTE_URL", "")
        remote.start()
        self.addCleanup(remote.stop)

    def test_add_persists_and_merges(self):
        cat = phr.add_custom("Greeting", "관리자가 추가한 인사말")
        self.assertEqual(cat, "greeting")
        self.assertIn("관리자가 추가한 인사말", phr.merged()["greeting"])
        saved = phr._db_phrases()
        self.assertEqual(saved["greeting"], ["관리자가 추가한 인사말"])

    def test_new_category_allowed(self):
        phr.add_custom("zone_b3", "B3 구역 안내 문구입니다")
        self.assertIn("zone_b3", phr.merged())

    def test_pick_includes_custom(self):
        phr.add_custom("only_custom", "유일한 문구")
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

    def test_db_phrases_tolerates_missing_and_corrupt(self):
        self.assertEqual(phr._db_phrases(), {})
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(phr._db_phrases(), {})

    def test_all_lines_marks_custom(self):
        phr.add_custom("greeting", "추가 표시 문구")
        rows = [(c, t, cu) for c, t, cu in phr.all_lines() if t == "추가 표시 문구"]
        self.assertEqual(rows, [("greeting", "추가 표시 문구", True)])


class NewScenarioTests(unittest.TestCase):
    def test_fire_evac_speaks_three_stages(self):
        ctx = FakeCtx()
        # 실제 문구 선택은 무작위다. 특정 단어가 뽑힐 때만 통과시키면 플랫폼과
        # 실행 순서에 따라 흔들리므로, 시나리오가 세 단계 카테고리를 정확히
        # 요청하는지를 고정해서 검증한다.
        with mock.patch.object(scenarios, "pick", side_effect=lambda category: category):
            scenarios.sc_fire_evac(ctx)
        self.assertEqual(ctx.lines, ["fire_detected", "fire_evac", "fire_report"])
        self.assertIn(("system", "화재 대피 유도 방송"), ctx.events)

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

    def test_fetch_status_parses_real_payload_shape(self):
        # 실제 /api/telemetry 페이로드: escalation 은 'L0' 문자열, telemetry 는
        # 중첩 객체이거나 아직 없으면 None. 이전엔 fetch_status 자체를 mock 해서
        # escalation 문자열에 .get 을 부르는 크래시를 놓쳤다.
        real_payload = {
            "type": "telemetry",
            "device_id": "mechdog-01",
            "state": "IDLE",
            "escalation": "L0",
            "telemetry": {"batt_v": 8.54, "temp_c": 41.2},
            "stale": False,
        }
        with mock.patch.object(robotlink, "_get", return_value=real_payload):
            st = robotlink.fetch_status()
        self.assertIn("8.54", st)
        self.assertIn("IDLE", st)
        self.assertIn("L0", st)

    def test_fetch_status_no_telemetry_yet(self):
        # 시뮬·링크 전 상태 — telemetry=None 이 와도 죽지 않는다.
        payload = {"state": "FAILSAFE", "escalation": "L3", "telemetry": None, "stale": True}
        with mock.patch.object(robotlink, "_get", return_value=payload):
            st = robotlink.fetch_status()
        self.assertIn("FAILSAFE", st)
        self.assertIn("L3", st)
        self.assertIn("링크 지연", st)

    def test_command_endings_match(self):
        # 자연 발화 어미 변형은 명령으로 간다.
        for phrase in (
            "비상정지해",
            "비상정지해줘",
            "비상정지해주세요",
            "순찰시작해",
            "스톱해라",
            "수동모드로전환해줘" if False else "수동제어해줘",
            "수동모드로전환해줘",
            "자동모드로바꿔",
            "수동모드로전환해",
            "순찰정지해",
            # 실제 합성 음성 STT에서 "해줘"가 아래 어미로 흔들렸다(#171).
            "비상정지해져",
            "비상정지해죠",
            "비상정지하죠",
            "비상정지했죠",
            "비상정지했어요",
            "비상정지했어",
            # 실제 합성 음성에서 "비상정지해줘"가 이렇게 인식된 사례만 허용한다.
            "비상정지에",
        ):
            self.assertIsNotNone(robotlink.match_action(phrase), phrase)

    def test_negation_and_condition_never_match(self):
        # 어미 목록에 없는 꼬리는 절대 명령이 안 된다 — 부정·의문 안전.
        for phrase in (
            "비상정지하지마",
            "비상정지할까",
            "순찰시작할까봐",
            "비상정지하면",
            "비상정지하자",
            "순찰해제",
            "순찰시작에",
            "비상정지에 대해 알려줘",
            "순찰시작에 문제가 있어",
        ):
            self.assertIsNone(robotlink.match_action(phrase), phrase)

    def test_partial_phrase_still_no_match(self):
        # "순찰해" 자체는 등록된 명령이지만, "순찰" 만으로는 안 된다.
        self.assertIsNone(robotlink.match_action("순찰"))
        self.assertIsNone(robotlink.match_action("정지"))
        self.assertIsNone(robotlink.match_action("비상"))


class TranscribeTests(unittest.TestCase):
    def test_domain_prompt_is_passed_to_whisper_without_changing_audio_result(self):
        seen = {}

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                seen["samples"] = len(audio)
                seen["kwargs"] = kwargs
                return [
                    types.SimpleNamespace(text=" 메카독 "),
                    types.SimpleNamespace(text="순찰 시작 "),
                ], {}

        text = vp.transcribe(FakeModel(), b"\x00\x00\xff\x7f")

        self.assertEqual(text, "메카독 순찰 시작")
        self.assertEqual(seen["samples"], 2)
        self.assertEqual(
            seen["kwargs"],
            {
                "language": "ko",
                "beam_size": 5,
                "vad_filter": True,
                "initial_prompt": vp.STT_PROMPT,
            },
        )
        self.assertIn("메카독", vp.STT_PROMPT)
        self.assertIn("비상정지", vp.STT_PROMPT)


class HubScenarioQueueTests(unittest.TestCase):
    def test_auth_prompt_once_per_wait(self):
        hub = vp.Hub("test")
        self.assertIsNone(hub.auth_prompt("PATROL"))
        self.assertEqual(hub.auth_prompt("AUTH_WAIT"), "멈췄습니다. 암구호를 말씀해 주세요.")
        self.assertIsNone(hub.auth_prompt("AUTH_WAIT"))
        self.assertIsNone(hub.auth_prompt(None))
        self.assertIsNone(hub.auth_prompt("IDLE"))
        self.assertEqual(hub.auth_prompt("AUTH_WAIT"), "멈췄습니다. 암구호를 말씀해 주세요.")

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


class HallucinationFilterTests(unittest.TestCase):
    """무음 환각 거름 — no_speech_prob 를 기록만 하면 소비처 없는 표식이다 (3.8.2)."""

    @staticmethod
    def _model(segments):
        class FakeModel:
            def transcribe(self, *_a, **_k):
                return segments, {}

        return FakeModel()

    def test_high_no_speech_prob_segment_is_dropped(self):
        segs = [
            types.SimpleNamespace(text=" 메카독 ", no_speech_prob=0.05),
            types.SimpleNamespace(text=" 오늘도 시청해 주셔서 감사합니다. ", no_speech_prob=0.753),
        ]
        text = vp.transcribe(self._model(segs), b"\x00\x00\xff\x7f")
        self.assertEqual(text, "메카독")

    def test_all_hallucinated_segments_return_empty(self):
        segs = [types.SimpleNamespace(text=" 감사합니다 ", no_speech_prob=0.9)]
        self.assertEqual(vp.transcribe(self._model(segs), b"\x00" * 4), "")

    def test_missing_no_speech_prob_attribute_is_kept(self):
        segs = [types.SimpleNamespace(text=" 메카독 ")]
        self.assertEqual(vp.transcribe(self._model(segs), b"\x00" * 4), "메카독")


class PassphraseTests(unittest.TestCase):
    """암구호 대조 — 이름 대조가 아니라 등록 **문구** 대조다 (WBS 3.8.2)."""

    def test_registered_phrase_inside_natural_speech_matches(self):
        self.assertTrue(vp.match_passphrase("암구호는 메카독 출입 허가 입니다"))

    def test_unregistered_speech_does_not_match(self):
        self.assertFalse(vp.match_passphrase("사원 홍길동입니다"))
        self.assertFalse(vp.match_passphrase("메카독 순찰 시작해"))

    def test_name_alone_is_not_a_passphrase(self):
        # 이름은 비밀이 아니다 — 명단 대조(4.7.7)와 다른 축이다.
        self.assertFalse(vp.match_passphrase("홍길동"))

    def test_configured_list_overrides_code_default(self):
        import json

        with mock.patch.object(vp.voice_store, "setting", return_value=json.dumps(["새 암구호"])):
            self.assertTrue(vp.match_passphrase("새 암구호입니다"))
            self.assertFalse(vp.match_passphrase("메카독 출입 허가"))

    def test_default_passphrase_is_matched_on_raw_speech(self):
        # ⚠️ 회귀 방지: 대조는 라우팅용으로 가공한 값이 아니라 **원문**으로
        # 해야 한다. 출고 기본 문구가 웨이크워드로 시작하므로, `_strip_wake()`
        # 값으로 대조하면 **문구를 정확히 말한 사람이 떨어지는** 역전이 난다.
        spoken = "메카독 출입 허가"
        self.assertTrue(vp.match_passphrase(spoken))
        self.assertFalse(vp.match_passphrase(vp._strip_wake(spoken)))

    def test_broken_setting_closes_auth_instead_of_falling_back(self):
        # 되돌리면 관리자가 JSON 이 아닌 값을 넣은 순간 저장소에 공개된
        # 데모 문구가 조용히 문을 열어 준다 — 바꿨다고 믿는 채로.
        with mock.patch.object(vp.voice_store, "setting", return_value="{깨짐"):
            self.assertEqual(vp.passphrases(), [])
            self.assertFalse(vp.match_passphrase("메카독 출입 허가"))

    def test_non_list_setting_closes_auth(self):
        import json

        with mock.patch.object(vp.voice_store, "setting", return_value=json.dumps("문구")):
            self.assertEqual(vp.passphrases(), [])

    def test_empty_entry_does_not_authenticate_everything(self):
        import json

        with mock.patch.object(vp.voice_store, "setting", return_value=json.dumps(["", "  "])):
            self.assertEqual(vp.passphrases(), [])
            self.assertFalse(vp.match_passphrase("아무 말이나"))


class AuthLinkTests(unittest.TestCase):
    """로봇 FSM 과의 연결 — 대조 결과만 보내고 인식 텍스트는 보내지 않는다."""

    def test_robot_state_returns_fsm_state(self):
        with mock.patch.object(robotlink, "_get", return_value={"state": "AUTH_WAIT"}) as get:
            self.assertEqual(robotlink.robot_state(), "AUTH_WAIT")
            get.assert_called_once()

    def test_robot_state_none_when_unreachable(self):
        with mock.patch.object(robotlink, "_get", side_effect=OSError):
            self.assertIsNone(robotlink.robot_state())

    def test_post_auth_result_sends_only_the_verdict(self):
        captured = {}

        def fake_post(_base, path, body, **_k):
            captured["path"], captured["body"] = path, body
            return {"accepted": True}

        with mock.patch.object(robotlink, "_post", side_effect=fake_post):
            ok, err = robotlink.post_auth_result(True)
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/api/command/auth")
        self.assertEqual(captured["body"], {"result": "ok"})
        self.assertNotIn("text", captured["body"])

    def test_post_auth_result_refusal_is_reported(self):
        refused = {"accepted": False, "detail": "IDLE 에서는 인증 결과를 받지 않는다"}
        with mock.patch.object(robotlink, "_post", return_value=refused):
            ok, err = robotlink.post_auth_result(False)
        self.assertFalse(ok)
        self.assertIn("받지 않는다", err)

    def test_post_auth_result_keeps_accepted_stale_detail(self):
        with mock.patch.object(
            robotlink, "_post", return_value={"accepted": True, "detail": "다시 말해 주세요"}
        ):
            ok, detail = robotlink.post_auth_result(True)
        self.assertTrue(ok)
        self.assertEqual(detail, "다시 말해 주세요")


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


class RouteQueryTests(unittest.TestCase):
    """우선순위 라우팅 — 구체 규칙이 넓은 단어 검사보다 먼저다."""

    def test_exact_command_beats_emergency_word(self):
        # "비상정지" 는 "비상" 을 포함하지만 전파가 아니라 정지 명령이다.
        self.assertEqual(vp.route_query("비상정지"), "action")
        self.assertEqual(vp.route_query("긴급정지"), "action")
        self.assertEqual(vp.route_query("스톱"), "action")
        self.assertEqual(vp.route_query("순찰시작"), "action")

    def test_scenario_trigger_beats_emergency_word(self):
        # "비상접수" 는 "비상" 을 포함하지만 접수 시나리오다.
        self.assertEqual(vp.route_query("비상접수"), "scenario")
        self.assertEqual(vp.route_query("화재대피"), "scenario")

    def test_plain_emergency_still_routes(self):
        self.assertEqual(vp.route_query("비상"), "emergency")
        self.assertEqual(vp.route_query("도와줘"), "emergency")
        self.assertEqual(vp.route_query("지금비상상황이야"), "emergency")

    def test_status_query(self):
        self.assertEqual(vp.route_query("배터리어때"), "status")
        self.assertEqual(vp.route_query("지금상태알려줘"), "status")

    def test_unrelated_goes_to_llm(self):
        self.assertEqual(vp.route_query("오늘점심뭐야"), "llm")
        self.assertEqual(vp.route_query(""), "llm")


if __name__ == "__main__":
    unittest.main()


class RobotEscalationWarningTests(unittest.TestCase):
    """단계가 오르면 경고를 읽는다 (WBS 3.5.6 · FR-3.4)."""

    def _poll(self, hub, events):
        original = vp.robotlink.fetch_events
        vp.robotlink.fetch_events = lambda _base, since: (events, 0, since + len(events))
        try:
            hub._robot_next_poll = 0
            hub.poll_robot_events("http://127.0.0.1:8000")
        finally:
            vp.robotlink.fetch_events = original

    def test_level_change_is_spoken_urgently(self):
        hub = vp.Hub("test")
        hub.enqueue_say("나중에 읽을 공지")
        self._poll(
            hub,
            [
                {
                    "event": "escalation_changed",
                    "state": "AUTH_WAIT",
                    "escalation": "L2",
                    "warning": "사원증을 보여 주십시오.",
                }
            ],
        )
        items = [item for _, _, item in hub.drain_say()]
        self.assertEqual(items[0], "사원증을 보여 주십시오.")  # 공지보다 앞이다

    def test_other_events_are_journaled_but_not_spoken(self):
        """⚠️ 사람 확정마다 말하면 순찰이 방송이 된다."""
        hub = vp.Hub("test")
        self._poll(hub, [{"event": "person_found", "state": "ALERT", "escalation": "L1"}])
        self.assertEqual(hub.drain_say(), [])
        self.assertTrue(any("person_found" in e["text"] for e in hub.events))

    def test_a_level_without_a_warning_says_nothing(self):
        """L0 복귀까지 읽으면 경보 해제가 새 방송이 된다."""
        hub = vp.Hub("test")
        self._poll(
            hub,
            [
                {
                    "event": "escalation_changed",
                    "state": "PATROL",
                    "escalation": "L0",
                    "warning": None,
                }
            ],
        )
        self.assertEqual(hub.drain_say(), [])
