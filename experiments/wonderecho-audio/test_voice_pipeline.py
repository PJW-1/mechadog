"""Scenario/robotlink/transport/phrases unit tests — no serial, mic, or GPU required."""

import contextlib
import io
import json
import math
import os
import struct
import sys
import threading
import time
import types
import unittest
from unittest import mock

import phrases as phr
import robotlink
import scenarios
import voice_pipeline as vp
import voice_rules


def setUpModule():
    # 개발자 PC 의 로컬 규칙 파일(voice_data.rules.json)이 아니라 코드 기본 규칙으로 시험한다.
    patch = mock.patch.object(
        voice_rules, "RULES_PATH", voice_rules.RULES_PATH.with_name("_absent_for_tests.rules.json")
    )
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class FakeCtx:
    """Records scenario side effects without hardware."""

    def __init__(self, answers=(), roster_docs=""):
        self.lines = []
        self.events = []
        self.answers = list(answers)
        self.docs = roster_docs
        self.commands = []

    def say(self, text):
        self.lines.append(text)

    def listen(self, _timeout_s=12.0):
        return self.answers.pop(0) if self.answers else ""

    def retrieve(self, _query):
        return self.docs

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

    def test_live_status_briefing_is_retired(self):
        # 실측 수치를 읽어 주는 브리핑은 폐기했다(ADR-38) — MP3 모듈은 미리 녹음한
        # 문장만 낸다. 트리거가 남으면 사라진 시나리오를 부르게 된다.
        self.assertNotIn("robot_briefing", scenarios.SCENARIOS)
        self.assertNotIn("robot_briefing", scenarios.TRIGGERS.values())


class GuardScenarioTests(unittest.TestCase):
    def test_guard_check_closes_com_port(self):
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
        self.assertTrue(any(line in phr.PHRASES["identity_ok"] for line in ctx.lines))
        # 이름은 말하지 않고(고정 문장만 TF 카드에 있다 · 4.7.21) 기록에만 남긴다
        self.assertFalse(any("김민수" in line for line in ctx.lines))
        self.assertTrue(any("김민수" in text for _, text in ctx.events))

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
        for cat, text in phr.all_lines():
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
        self.assertFalse(any("3번 창고" in line for line in ctx.lines))
        self.assertTrue(any("3번 창고" in text for _, text in ctx.events))

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

    def test_answer_query_passes_non_commands_through(self):
        # 상태 질의도 더는 여기서 답하지 않는다(ADR-38). 로봇 API 를 부르지 않는다.
        with mock.patch.object(robotlink, "_get") as get:
            for query in ("회사 복지 제도가 뭐야", "배터리 어때", "상태 알려줘"):
                handled, _ = robotlink.answer_query(query)
                self.assertFalse(handled, query)
        get.assert_not_called()

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

    def test_prompt_is_vocabulary_only(self):
        # 흐린 소리를 Whisper 가 힌트 문장으로 채운다 — 명령어 아닌 문장을 두지 않는다.
        self.assertNotIn("음성 명령", vp.STT_PROMPT)
        for word in vp.STT_PROMPT.rstrip(".").split(","):
            self.assertNotIn(" ", word.strip(), word)


class HubScenarioQueueTests(unittest.TestCase):
    def test_auth_prompt_once_per_wait(self):
        hub = vp.Hub("test")
        self.assertIsNone(hub.auth_prompt("PATROL"))
        self.assertEqual(
            hub.auth_prompt("AUTH_WAIT"),
            "인증되지 않은 사람이 확인되었습니다. 암구호를 말씀해 주십시오.",
        )
        self.assertIsNone(hub.auth_prompt("AUTH_WAIT"))
        self.assertIsNone(hub.auth_prompt(None))
        self.assertIsNone(hub.auth_prompt("IDLE"))
        self.assertEqual(
            hub.auth_prompt("AUTH_WAIT"),
            "인증되지 않은 사람이 확인되었습니다. 암구호를 말씀해 주십시오.",
        )

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

    def _post(self, hub, path, body):
        h = vp.make_handler(hub).__new__(vp.make_handler(hub))
        sent = []
        h.path, h.headers = path, {"Content-Length": str(len(body))}
        h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
        h.request_version = "HTTP/1.1"
        h.send_response = sent.append
        h.send_header = lambda *_a: None
        h.end_headers = lambda: None
        h.do_POST()
        return sent[0]

    def test_typed_broadcast_endpoint_is_retired(self):
        # 관제 화면의 임의 문장 방송(/say)은 폐기했다(ADR-38). 단계 경고는 같은
        # 큐를 쓰지만 사건 폴링으로만 들어온다.
        hub = vp.Hub("test")
        self.assertEqual(self._post(hub, "/say", '{"text": "아무 문장"}'.encode()), 404)
        self.assertEqual(hub.say_q.qsize(), 0)

    def test_phrase_editing_endpoints_are_retired(self):
        # 출력은 TF 카드에 미리 녹음한 문장뿐이라 문구 추가·삭제는 없다(4.7.22).
        body = '{"category": "greeting", "text": "새 문구"}'.encode()
        for path in ("/phrases", "/phrases/delete"):
            with self.subTest(path=path):
                self.assertEqual(self._post(vp.Hub("test"), path, body), 404)


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

    def test_low_confidence_prompt_echo_is_dropped(self):
        # 4.7.19 ⑤ 실측: 흐린 1 m 녹음이 힌트 어휘로 지어내졌다 (no_speech 0.47, logprob -0.91).
        segs = [
            types.SimpleNamespace(
                text=" 메카독, 비상정지. ", no_speech_prob=0.47, avg_logprob=-0.91
            )
        ]
        self.assertEqual(vp.transcribe(self._model(segs), b"\x00" * 4), "")

    def test_dropped_text_is_not_printed(self):
        # 인증 대기 중에는 발화가 곧 암구호다 — 버린 세그먼트도 원문을 찍지 않는다.
        segs = [
            types.SimpleNamespace(text=" 비밀문구가 ", no_speech_prob=0.9, avg_logprob=-0.2),
            types.SimpleNamespace(text=" 새면 안 된다 ", no_speech_prob=0.1, avg_logprob=-0.9),
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(vp.transcribe(self._model(segs), b"\x00" * 4), "")
        self.assertIn("버림", out.getvalue())
        self.assertNotIn("비밀", out.getvalue())
        self.assertNotIn("새면", out.getvalue())

    def test_real_short_command_is_kept(self):
        # 실제 명령 중 가장 낮았던 값 ("멈춰." -0.48)
        segs = [types.SimpleNamespace(text=" 멈춰. ", no_speech_prob=0.02, avg_logprob=-0.48)]
        self.assertEqual(vp.transcribe(self._model(segs), b"\x00" * 4), "멈춰.")


class PassphraseTests(unittest.TestCase):
    """암구호 대조 — 이름 대조가 아니라 등록 **문구** 대조다 (WBS 3.8.2)."""

    def setUp(self):
        # 개발자 PC 에 실제 암구호가 설정돼 있어도 코드 기본값으로 시험한다.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(vp.PASSPHRASES_ENV, None)

    def _env(self, value):
        return mock.patch.dict(os.environ, {vp.PASSPHRASES_ENV: value})

    def test_registered_phrase_inside_natural_speech_matches(self):
        self.assertTrue(vp.match_passphrase("암구호는 메카독 출입 허가 입니다"))

    def test_unregistered_speech_does_not_match(self):
        self.assertFalse(vp.match_passphrase("사원 홍길동입니다"))
        self.assertFalse(vp.match_passphrase("메카독 순찰 시작해"))

    def test_name_alone_is_not_a_passphrase(self):
        # 이름은 비밀이 아니다 — 명단 대조(4.7.7)와 다른 축이다.
        self.assertFalse(vp.match_passphrase("홍길동"))

    def test_configured_list_overrides_code_default(self):
        with self._env(json.dumps(["새 암구호"])):
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
        with self._env("{깨짐"):
            self.assertEqual(vp.passphrases(), [])
            self.assertFalse(vp.match_passphrase("메카독 출입 허가"))

    def test_non_list_setting_closes_auth(self):
        with self._env(json.dumps("문구")):
            self.assertEqual(vp.passphrases(), [])

    def test_unset_env_uses_public_demo_phrase(self):
        self.assertEqual(vp.passphrases(), list(vp.DEFAULT_PASSPHRASES))

    def test_empty_entry_does_not_authenticate_everything(self):
        with self._env(json.dumps(["", "  "])):
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

    def test_robot_state_level_returns_state_and_escalation(self):
        snap = {"state": "ALERT", "escalation": "L3"}
        with mock.patch.object(robotlink, "_get", return_value=snap):
            self.assertEqual(robotlink.robot_state_level(), ("ALERT", "L3"))

    def test_robot_state_level_none_pair_when_unreachable(self):
        with mock.patch.object(robotlink, "_get", side_effect=OSError):
            self.assertEqual(robotlink.robot_state_level(), (None, None))

    def test_robot_state_level_ignores_non_string_fields(self):
        with mock.patch.object(robotlink, "_get", return_value={"state": 3, "escalation": None}):
            self.assertEqual(robotlink.robot_state_level(), (None, None))


class BadgeVerdictTests(unittest.TestCase):
    """사원증 확인 발화 조건 — `PATROL` 을 목격하던 레이스를 걸어낸 자리."""

    def test_waits_while_still_in_auth_wait(self):
        self.assertEqual(vp.badge_verdict("AUTH_WAIT", "L2"), "wait")

    def test_waits_when_runtime_unreachable(self):
        self.assertEqual(vp.badge_verdict(None, None), "wait")

    def test_announces_on_patrol(self):
        self.assertEqual(vp.badge_verdict("PATROL", "L0"), "announce")

    def test_announces_even_if_alert_came_first(self):
        """인증한 사람이 그대로 서 있으면 `PATROL` 은 1~2초만에 `ALERT` 가 된다."""
        self.assertEqual(vp.badge_verdict("ALERT", "L1"), "announce")

    def test_drops_on_auth_failed(self):
        """`AUTH_FAILED` 도 `AUTH_WAIT` 를 나간다 — 실패에 확인 발화를 하면 안 된다."""
        self.assertEqual(vp.badge_verdict("ALERT", "L3"), "drop")

    def test_drops_on_failsafe(self):
        self.assertEqual(vp.badge_verdict("FAILSAFE", "F"), "drop")

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


class FakeMic:
    """XiaoMic stand-in: hands out scripted 20 ms frames, then silence of the link."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.flushed = 0

    def flush(self):
        self.flushed += 1

    def read_frame(self, timeout):
        if self.frames:
            return self.frames.pop(0)
        time.sleep(timeout)  # 링크가 멈춘 것처럼 — 프레임이 오지 않는다
        return None


def _frame(level, hz=1000):
    """20 ms 사인파 — VAD 는 말소리 대역만 보므로 직류로는 말소리를 흉내 낼 수 없다."""
    return struct.pack(
        "<320h", *(round(level * math.sin(2 * math.pi * hz * i / 16000)) for i in range(320))
    )


def _mix(*frames):
    return struct.pack(
        "<320h", *(sum(xs) for xs in zip(*(struct.unpack("<320h", f) for f in frames), strict=True))
    )


class XiaoCaptureTests(unittest.TestCase):
    """WBS 4.7.19 — XIAO 마이크도 WonderEcho 와 같은 VAD 계약을 지킨다."""

    def test_echo_guard_skips_prompt_tail_then_vad_ends_turn(self):
        guard = int(vp.ECHO_GUARD_S * 1000) // 20
        echo, quiet, loud = [_frame(3000)] * guard, [_frame(0)] * 30, [_frame(3000)] * 10
        mic = FakeMic(echo + quiet + loud + [_frame(0)] * 60)
        heard = []
        pcm, speech, at_ms = vp.capture_xiao(mic, timeout_s=5.0, on_speech=heard.append)
        self.assertEqual(mic.flushed, 1)  # 쉬지 않는 마이크 — 지난 소리를 버리고 시작
        self.assertTrue(speech)
        self.assertEqual(heard, [at_ms])  # 말이 시작된 프레임에서 한 번만
        self.assertEqual(pcm[:640], _frame(0))  # 안내 멘트 꼬리는 녹음에 없다
        self.assertEqual(len(pcm), (30 + 10 + 60) * 640)

    def test_link_stall_after_speech_still_ends_the_turn(self):
        mic = FakeMic([_frame(0)] * 40 + [_frame(3000)] * 10)
        t0 = time.monotonic()
        pcm, speech, _ = vp.capture_xiao(mic, timeout_s=10.0, guard_s=0)
        self.assertTrue(speech)
        self.assertLess(time.monotonic() - t0, 3.0)  # 10초를 다 기다리지 않는다
        self.assertEqual(len(pcm), 50 * 640)

    def test_silence_is_not_speech(self):
        pcm, speech, at_ms = vp.capture_xiao(FakeMic([_frame(0)] * 50), timeout_s=0.5, guard_s=0)
        self.assertFalse(speech)
        self.assertIsNone(at_ms)
        self.assertEqual(len(pcm), 50 * 640)

    def test_out_of_band_noise_is_not_speech(self):
        # 로봇에 올린 XIAO 의 소음: 100 Hz 아래 흔들림과 4–8 kHz 서보음 (4.7.19 ⑤).
        # 전 대역 rms 로는 문턱을 크게 넘는 세기다.
        dc = struct.pack("<320h", *([3000] * 320))  # 직류 치우침 — 사인파로는 0 Hz 를 못 만든다
        for name, loud in (("dc", dc), ("50Hz", _frame(3000, 50)), ("6kHz", _frame(3000, 6000))):
            with self.subTest(noise=name):
                frames = [_frame(0)] * 30 + [loud] * 20
                _, speech, _ = vp.capture_xiao(FakeMic(frames), timeout_s=0.5, guard_s=0)
                self.assertFalse(speech)

    def test_quiet_speech_over_out_of_band_noise_is_heard(self):
        # 1 m 발화처럼 약한 말소리도 대역 밖 소음 위에서 잡힌다.
        noise = _mix(_frame(1500, 50), _frame(1500, 6000))
        mic = FakeMic([noise] * 30 + [_mix(noise, _frame(400))] * 10 + [noise] * 60)
        _, speech, _ = vp.capture_xiao(mic, timeout_s=5.0, guard_s=0)
        self.assertTrue(speech)

    def test_wake_before_speech_ends_the_wait_without_link_error(self):
        mic = FakeMic([_frame(0)] * 500)
        t0 = time.monotonic()
        pcm, speech, at_ms = vp.capture_xiao(
            mic, timeout_s=10.0, guard_s=1.0, interrupt=lambda: True
        )
        self.assertLess(time.monotonic() - t0, 1.0)  # 경고가 기다리는데 10초를 다 쓰지 않는다
        self.assertFalse(speech)
        self.assertIsNone(at_ms)

    def test_wake_cuts_even_after_speech_started(self):
        # 실기에서 주변 소음이 매 턴 말소리로 잡혀(captured 14.7s) 「말 시작 전에만 끊기」 가 소용없었다.
        mic = FakeMic([_frame(3000)] * 200)
        t0 = time.monotonic()
        _, speech, _ = vp.capture_xiao(
            mic, timeout_s=10.0, guard_s=0, interrupt=lambda: len(mic.frames) < 150
        )
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(speech)  # 받던 말은 버린다 — 곧 로봇이 말한다

    def test_dead_link_raises_timeout_like_serial_path(self):
        with self.assertRaises(TimeoutError):
            vp.capture_xiao(FakeMic([]), timeout_s=0.3)

    def test_listen_pcm_prefers_xiao(self):
        with (
            mock.patch.object(vp, "capture_xiao", return_value="xiao") as xiao,
            mock.patch.object(vp, "capture_pcm", return_value="serial") as serial_cap,
        ):
            self.assertEqual(vp.listen_pcm("dev", "dec", "mic", 3.0), "xiao")
            self.assertEqual(vp.listen_pcm("dev", "dec", None, 3.0), "serial")
            vp.listen_pcm(None, None, "mic", 3.0)
        # 에코 가드는 멘트를 내보내는 스피커(--port)가 있을 때만 건다
        self.assertEqual([c.kwargs["guard_s"] for c in xiao.call_args_list], [vp.ECHO_GUARD_S, 0.0])
        serial_cap.assert_called_once_with("dev", "dec", timeout_s=3.0, on_speech=None)

    def test_status_carries_drop_counters(self):
        hub = vp.Hub("r1")
        self.assertIsNone(hub.snapshot()["mic"])
        hub.mic = mock.Mock(stats=lambda: {"drops": 2, "connects": 3})
        self.assertEqual(hub.snapshot()["mic"]["drops"], 2)

    def test_say_without_speaker_only_prints(self):
        with mock.patch.object(vp, "stream_play") as play:
            vp._say(None, object(), "안내", 1.0)
        play.assert_not_called()


class RobotSpeakerTests(unittest.TestCase):
    """--robot-speaker — 문장을 TF 카드 트랙으로 바꿔 로봇 MP3 모듈로 튼다 (WBS 4.7.21)."""

    def setUp(self):
        patcher = mock.patch.object(vp, "ROBOT_SPEAKER", "http://api")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _say(self, text, track, played=(True, "")):
        with (
            mock.patch.object(vp.tf_tracks, "track_for", return_value=track),
            mock.patch.object(vp.robotlink, "play_track", return_value=played) as play,
            mock.patch.object(vp, "synth_piper", return_value=b"\0" * 32000) as synth,
            mock.patch.object(vp, "stream_play") as serial_play,
            mock.patch.object(vp.time, "sleep") as sleep,
        ):
            vp._say("dev", "piper", text, 1.2)
        serial_play.assert_not_called()  # 로봇 스피커가 있으면 WonderEcho 로 말하지 않는다
        return play, synth, sleep

    def test_plays_the_table_track_and_waits_for_it_to_finish(self):
        play, synth, sleep = self._say("안내", 7)
        play.assert_called_once_with(7, "http://api")
        synth.assert_called_once_with("piper", "안내", 1.2)  # 같은 모델·속도 = 카드 음원 길이
        (waited,) = sleep.call_args.args
        self.assertGreater(waited, 1.0)  # 1초 음원 + 여유 — 로봇 마이크가 제 말을 듣지 않게
        self.assertLess(waited, 1.0 + vp.ROBOT_SPEAKER_TAIL_S + 0.01)

    def test_sentence_missing_from_the_table_plays_the_fallback_line(self):
        """서버가 돌려준 거부 사유처럼 미리 녹음할 수 없는 문장은 고정 대체 문장으로 튼다."""
        fallback = vp.pick("unplayable")
        tracks = {fallback: 184}
        with (
            mock.patch.object(vp.tf_tracks, "track_for", side_effect=tracks.get),
            mock.patch.object(vp.robotlink, "play_track", return_value=(True, "")) as play,
            mock.patch.object(vp, "synth_piper", return_value=b"") as synth,
            mock.patch.object(vp.time, "sleep"),
        ):
            vp._say(None, "piper", "자율 동작 중이 아니다", 1.2)
        play.assert_called_once_with(184, "http://api")
        synth.assert_called_once_with("piper", fallback, 1.2)  # 기다리는 길이도 대체 문장 기준

    def test_refused_track_does_not_wait(self):
        _, _, sleep = self._say("안내", 7, played=(False, "연결 안 됨"))
        sleep.assert_not_called()

    def test_xiao_echo_guard_applies_to_the_robot_speaker(self):
        with mock.patch.object(vp, "capture_xiao") as xiao:
            vp.listen_pcm(None, None, "mic", 3.0)
        self.assertEqual(xiao.call_args.kwargs["guard_s"], vp.ECHO_GUARD_S)

    def test_scenario_speaks_without_a_serial_device(self):
        args = types.SimpleNamespace(speed=1.2)
        ctx = vp.ScenarioCtx(None, None, None, "piper", vp.Hub("r1"), [], args)
        with mock.patch.object(vp, "_say") as say:
            ctx.say("안내")
        say.assert_called_once_with(None, "piper", "안내", 1.2)


class XiaoStartupTests(unittest.TestCase):
    def _main(self, argv, mic=None):
        piper = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(load=lambda _path: object()))
        whisper = types.SimpleNamespace(WhisperModel=lambda *_a, **_kw: object())
        with (
            mock.patch.object(sys, "argv", ["voice_pipeline.py", *argv]),
            mock.patch.dict(sys.modules, {"piper": piper, "faster_whisper": whisper}),
            mock.patch.object(vp, "open_transport") as transport,
            mock.patch.object(vp, "open_mic", return_value=mic),
            mock.patch.object(scenarios, "sc_guard") as guard,
        ):
            vp.main()
        return transport, guard

    def test_guard_check_listens_through_xiao_without_com_port(self):
        mic = mock.Mock()
        transport, guard = self._main(["--xiao", "10.0.0.9", "--guard-check"], mic=mic)
        transport.assert_not_called()  # 말하기 장치 없이도 기동한다
        ctx = guard.call_args.args[0]
        self.assertIs(ctx.mic, mic)
        self.assertIsNone(ctx.device)
        mic.stop.assert_called_once()

    def test_needs_some_microphone(self):
        with self.assertRaises(SystemExit):
            self._main(["--guard-check"])

    def test_say_still_needs_the_speaker_port(self):
        with self.assertRaises(SystemExit):
            self._main(["--xiao", "10.0.0.9", "--say", "안내"])

    def test_say_refuses_robot_speaker_instead_of_ignoring_it(self):
        # --say 는 임의 문장을 WonderEcho 로 흘린다. 카드 트랙으로는 못 트니 조용히 무시하지 않고 거절한다.
        with self.assertRaises(SystemExit):
            self._main(["--port", "COM3", "--robot-speaker", "--say", "안내"])


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

    def test_status_questions_are_not_answered(self):
        # 실측 상태 음성 응답은 폐기했다(ADR-38). 고정 문구로 답하는 경로로 간다.
        for query in ("배터리어때", "지금상태알려줘", "거리얼마야", "앞에장애물있어"):
            self.assertEqual(vp.route_query(query), "unknown", query)

    def test_unrelated_goes_to_fixed_reply(self):
        # LLM 은 없다 — 규칙에 걸리지 않은 발화는 고정 문구 하나로 답한다.
        self.assertEqual(vp.route_query("오늘점심뭐야"), "unknown")
        self.assertEqual(vp.route_query(""), "unknown")
        self.assertEqual(len(phr.PHRASES["not_understood"]), 1)

    def test_llm_is_gone(self):
        for name in ("SYSTEM", "reply", "for_speech", "machine_guard", "synth_orpheus"):
            self.assertFalse(hasattr(vp, name), name)


if __name__ == "__main__":
    unittest.main()


class RobotEscalationWarningTests(unittest.TestCase):
    """단계가 오르면 경고를 읽는다 (WBS 3.5.6 · FR-3.4)."""

    def _poll(self, hub, events):
        original = vp.robotlink.fetch_events
        vp.robotlink.fetch_events = lambda _base, since: (events, 0, since + len(events))
        try:
            hub._robot_next_poll = 0
            hub._robot_synced = True
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

    def test_wake_only_when_there_is_something_to_say(self):
        # 2026-09-24 실기: 말소리 대기 15초가 끝나야 폴링해서 L2 안내·L3 경고가 11초 늦었다.
        # 대기를 끊으면 받던 말을 버리므로, 곧 말할 것이 있을 때만 끊는다.
        l3 = {
            "event": "escalation_changed",
            "escalation": "L3",
            "warning": "경보가 발령되었습니다.",
        }
        cases = (
            ("검출", {"event": "person_found"}, False, False),
            ("문장 없는 단계", {"event": "escalation_changed", "escalation": "L1"}, False, False),
            ("경고 문장", l3, False, True),
            ("안내 전 인증 요구", {"event": "auth_required"}, False, True),
            (
                "안내 뒤 인증 요구",
                {"event": "auth_required"},
                True,
                False,
            ),  # 암구호를 말하는 중일 수 있다
        )
        for name, ev, prompted, wakes in cases:
            with self.subTest(name):
                hub = vp.Hub("test")
                hub.auth_prompted = prompted
                self._poll(hub, [ev])
                self.assertEqual(hub.wake.is_set(), wakes)

    def test_poller_survives_a_bad_response(self):
        # 폴링이 스레드로 옮겨 가며 예외 하나에 조용히 죽으면 그 뒤 경고가 영영 안 나간다.
        hub = vp.Hub("test")
        hub._robot_synced = True
        l3 = {
            "event": "escalation_changed",
            "escalation": "L3",
            "warning": "경보가 발령되었습니다.",
        }
        replies = [ValueError("깨진 JSON"), ([l3], 0, 1)]

        def fetch(_base, _since):
            r = replies.pop(0) if replies else ([], 0, 1)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(vp.robotlink, "fetch_events", fetch):
            threading.Thread(
                target=hub.poll_robot_events_forever, args=("http://x", 0.01), daemon=True
            ).start()
            self.assertTrue(hub.wake.wait(2.0))

    def test_first_poll_after_start_does_not_replay_old_warnings(self):
        # 2026-09-24: 음성을 재시작하자 7분 전 L3 경고를 다시 읽었다 — 커서 0 이 버퍼 전체를 받는다.
        hub = vp.Hub("test")
        old = [
            {"event": "escalation_changed", "escalation": "L3", "warning": "경보가 발령되었습니다."}
        ]
        original = vp.robotlink.fetch_events
        vp.robotlink.fetch_events = lambda _base, since: (old if since == 0 else [], 0, 1)
        try:
            hub.poll_robot_events("http://127.0.0.1:8000")
        finally:
            vp.robotlink.fetch_events = original
        self.assertEqual(hub.robot_cursor, 1)
        self.assertEqual(hub.drain_say(), [])
        self.assertFalse(hub.wake.is_set())

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
