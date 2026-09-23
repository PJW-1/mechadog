"""End-to-end voice loop: mic -> Whisper STT -> rules -> Piper TTS -> speaker.

All judgement lives on the PC. This script wires the verified pieces together:

    stream_client   UART <- Speex capture (command 0x108, 5 s window)
    faster-whisper  Korean speech -> text
    rules           wake/sleep words, whitelisted robot commands, scenarios,
                    emergency words, voice passphrase (no LLM — ADR-38)
    piper           fixed reply line -> 16 kHz waveform
    play_client     16 kHz PCM -> PLAY_DATA packets -> module speaker (firmware 2.1.23+)

An utterance that no rule claims gets one fixed line ("잘 못 들었습니다 ...").
The free-form local LLM was retired on 2026-09-23 together with spoken live
status reports and typed broadcasts (ADR-38): the target speaker is an MP3
module that only plays pre-recorded lines.

Transport note: the serial port (--port, e.g. COM5) is a **temporary**
validation link to the WonderEcho module. Relaying audio through the robot's
4-pin I2C bridge turned out to be impossible (2026-09-23, WBS 4.7.9 — the
bridge carries command ids only). The target path is: listen through the
XIAO ESP32S3 microphone (`http://<xiao>:82/audio`, 16 kHz PCM16 — `--xiao`,
WBS 4.7.19) and speak through the robot's MP3 module (`--robot-speaker`, WBS 4.7.21:
each line is looked up in tf_tracks.tsv and played as a TF card track).
With `--xiao` and no `--port`, replies are printed instead of spoken. Everything above the transport
boundary — the rules, the knowledge retrieval and the --web API — does not
change with it. The serial port is owned exclusively by the main loop: one
owner serializes audio frames so they never interleave.

Usage:
    python voice_pipeline.py --port COM8 --web 8090
    python voice_pipeline.py --xiao 192.168.1.102 --port COM8 --web 8090  # XIAO 로 듣기
    python voice_pipeline.py --port COM8 --say "안내 문장"     # play once, exit
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import daily_report
import eventlog
import phrases
import robotlink
import scenarios
import serial  # noqa: F401  (type only; stream_client already imports it)
import tf_tracks
import voice_rules
from phrases import pick
from play_client import CHUNK, PREFILL, end_packet, play_packet
from stream_client import STATUS, Decoder, make_decoder
from transport import open_transport
from xiao_mic import XiaoMic

READY, STARTED, FINISHED = 1, 2, 3

# 음성 명령: "메카독 ..."으로 시작해야만 대답한다.
# "그만/대기"는 대기 모드 전환(프로그램 종료는 Ctrl+C만 — 시연 중 오작동 방지),
# 대기 중에는 "메카독 시작/깨워/일어나"로만 복귀한다.
# 아래 호출어 상수들은 기본값(정본)이다 — 규칙 파일(voice_data.rules.json)이
# 있으면 같은 이름의 목록이 우선한다 (voice_rules 참조).
WAKE_PREFIXES = ("메카독", "메카도", "메카닭", "메카도기")  # STT 변형 흡수
SLEEP_WORDS = ("그만", "꺼져", "대기해", "잠자")
RESUME_WORDS = ("시작", "깨워", "일어나", "켜져")
EMERGENCY_WORDS = ("도와줘", "살려줘", "비상", "응급")
FOLLOW_S = 20.0  # 답변 후 이 시간 안의 발화는 웨이크워드 없이 받는다 (후속 대화)
FOLLOW_MIN_CHARS = 3  # 후속 창에서 이 길이 미만의 발화는 잡음으로 본다
_PUNCT = re.compile(r"[\s,.!?~…:'\"·]+")
KNOW_DIR = Path(__file__).with_name("knowledge")
# --robot-speaker 일 때 트랙 재생을 보낼 관제 API 베이스. None 이면 WonderEcho(--port)로 말한다.
ROBOT_SPEAKER = None
# 트랙 요청 → 틱 송신 → I2C 쓰기 지연과 MP3 앞뒤 무음. 로봇 마이크가 말끝을 듣으면 늘린다.
ROBOT_SPEAKER_TAIL_S = 0.5


class Hub:
    """Shared state between the voice loop (serial owner) and the web API.

    Only the main loop touches the serial port; queued items are drained
    between listening turns. `say` items (단계 경고 문장, 3.5.6) play even in
    standby — 대기 중인 로봇에서도 경고는 나가야 하므로. 관제 화면에서 임의
    문장을 방송하던 `/say` 는 폐기했다(ADR-38).
    """

    def __init__(self, robot_id, journal=None):
        self.robot_id = robot_id
        self.mode = "active"  # active | standby
        self.activity = "boot"  # listening | thinking | speaking | idle
        self.events = deque(maxlen=200)
        self.say_q = queue.PriorityQueue()
        self._seq = 0
        self.lock = threading.Lock()
        self._journal = journal  # EventJournal | None — 날짜별 JSONL 영속 기록
        self.robot_cursor = 0  # 관제 /api/events 폴링 커서
        self._robot_next_poll = 0.0
        self.auth_prompted = False
        self.mic = None  # XiaoMic | None — /status 에 끊김 계수를 싣는다 (4.7.19)

    def auth_prompt(self, state):
        """새 인증 대기마다 한 번 안내하고, 상태 조회 실패에는 중복 안내하지 않는다.

        ⚠️ **L2 경고 문장이 여기 있다** (WBS 3.5.6 · 2026-09-23 정정). 단계 쪽
        `escalation.sound.l2_warning` 은 `null` 이다 — 이 안내가 곧 `auth_prompted`
        를 세워 「안내 전 발화는 시도로 세지 않는다」 를 만들기 때문에, 문장만 단계
        쪽으로 옮기면 사건 폴링(5초) 만큼 **묻기 전에 게이트만 열리는 창**이 생긴다.

        ⚠️ **암구호를 먼저 묻는다.** `auth.require_both` 가 참이면 암구호가 통과하기
        전의 사원증은 판정하지 않는다(`runtime._judge_auth`). 사원증을 먼저 요구하면
        보여 줘도 아무 일이 일어나지 않는다.
        """
        if state == "AUTH_WAIT" and not self.auth_prompted:
            self.auth_prompted = True
            return "인증되지 않은 사람이 확인되었습니다. 암구호를 말씀해 주십시오."
        if state not in ("AUTH_WAIT", None):
            self.auth_prompted = False
        return None

    def event(self, role, text):
        self.events.append(
            {
                "ts": time.strftime("%H:%M:%S"),
                "role": role,  # user | robot | admin | system
                "text": text,
            }
        )
        if self._journal is not None:
            self._journal.record(role, text)  # 실패해도 루프는 멈추지 않는다

    def report_today(self):
        """오늘 날짜의 저널을 집계한다. 저널이 없으면 None."""
        if self._journal is None:
            return None
        date = time.strftime("%Y-%m-%d")
        return daily_report.summarize(eventlog.load_events(self._journal.path_for(date)), date=date)

    def poll_robot_events(self, base, interval=5.0):
        """관제 `/api/events` 커서 폴링 → 로봇 측 사건을 전사·저널에 합친다 (4.7.12).

        메인 루프의 턴 사이에 부른다. 연결 실패는 조용히 넘긴다 — 관제가 꺼져
        있어도 음성 루프는 살아야 한다. `dropped` 가 있으면 유실 사실을 남긴다.
        """
        now = time.monotonic()
        if now < self._robot_next_poll:
            return
        self._robot_next_poll = now + interval
        res = robotlink.fetch_events(base, self.robot_cursor)
        if res is None:
            return
        events, dropped, latest = res
        if dropped:
            # /ws/events 의 event_gap 과 같은 의미 — 조용히 넘기면 기록에 빈틈이 생긴다
            self.event("robot_evt", f"event_gap (dropped={dropped})")
        for e in events:
            kind = e.get("event", "?")
            state = e.get("state") or "?"
            esc = e.get("escalation") or "?"
            self.event("robot_evt", f"{kind} (state={state} 단계={esc})")
            # 3.5.6 — 단계가 오른 그 순간에만 경고를 읽는다. 중복 억제는 호스트가
            # 이미 했다(`_announce_escalation` 의 엣지). 여기서 다시 세지 않는다.
            # 문장을 여기 적지 않는 것도 같은 이유다 — 단계를 고칠 때 문구가 남는다.
            warning = e.get("warning")
            if kind == "escalation_changed" and warning:
                self.enqueue_say(str(warning), urgent=True)
        self.robot_cursor = latest

    def enqueue_say(self, text, urgent=False):
        with self.lock:
            self._seq += 1
            self.say_q.put((0 if urgent else 1, self._seq, text))
        self.event("admin", text)

    def drain_say(self):
        items = []
        while True:
            try:
                items.append(self.say_q.get_nowait())
            except queue.Empty:
                return items

    def enqueue_scenario(self, name, urgent=False):
        with self.lock:
            self._seq += 1
            self.say_q.put((0 if urgent else 1, self._seq, ("scenario", name)))
        self.event("system", f"시나리오 요청: {name}")

    def snapshot(self):
        return {
            "robot": self.robot_id,
            "mode": self.mode,
            "activity": self.activity,
            "say_queue": self.say_q.qsize(),
            "scenarios": list(scenarios.SCENARIOS),
            "events": list(self.events)[-30:],
            "mic": self.mic.stats() if self.mic is not None else None,
        }


# 독립 음성 페이지는 없다 — 음성 기능은 관제 대시보드(:8000 의 음성 중계 패널)에
# 통합됐고, 이 서버는 대시보드가 부르는 JSON API 만 제공한다.
DASHBOARD_URL = "http://127.0.0.1:8000/#voice"


def _api(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    _cors(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _cors(handler):
    """관제웹(다른 로컬 포트에서 서빙)이 이 API를 부를 수 있게 로컬 출처만 허용."""
    origin = handler.headers.get("Origin", "")
    if origin.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def make_handler(hub):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/status":
                _api(self, 200, hub.snapshot())
            elif self.path == "/scenarios":
                _api(
                    self,
                    200,
                    [{"name": n, "desc": d} for n, (d, _) in scenarios.SCENARIOS.items()],
                )
            elif self.path == "/phrases":
                cats = {}
                for cat, text in phrases.all_lines():
                    cats.setdefault(cat, []).append({"text": text})
                _api(
                    self,
                    200,
                    [
                        {"category": cat, "count": len(lines), "lines": lines}
                        for cat, lines in cats.items()
                    ],
                )
            elif self.path == "/transcript":
                _api(self, 200, list(hub.events))
            elif self.path == "/report":
                report = hub.report_today()
                _api(
                    self,
                    200 if report is not None else 404,
                    report if report is not None else {"error": "journal off"},
                )
            elif self.path == "/":
                # 독립 패널 폐지 — 음성 화면은 관제 대시보드로 통합됐다.
                self.send_response(302)
                self.send_header("Location", DASHBOARD_URL)
                self.end_headers()
            else:
                _api(self, 404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                _api(self, 400, {"error": "bad json"})
                return
            if self.path == "/scenario":
                name = (req.get("name") or "").strip()
                if name not in scenarios.SCENARIOS:
                    _api(
                        self,
                        404,
                        {"error": "unknown scenario", "scenarios": list(scenarios.SCENARIOS)},
                    )
                    return
                hub.enqueue_scenario(name, req.get("urgent"))
                _api(self, 200, {"queued": hub.say_q.qsize()})
            elif self.path == "/mode":
                mode = req.get("mode")
                if mode in ("active", "standby"):
                    hub.mode = mode
                elif mode is None:
                    hub.mode = "standby" if hub.mode == "active" else "active"
                else:
                    _api(self, 400, {"error": "mode must be active|standby"})
                    return
                _api(self, 200, {"mode": hub.mode})
            else:
                _api(self, 404, {"error": "not found"})

        def do_OPTIONS(self):
            self.send_response(204)
            _cors(self)
            self.end_headers()

        def log_message(self, *a):
            pass

    return H


def start_web(hub, port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(hub))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[web] http://0.0.0.0:{port} — /status /scenario /mode /transcript")
    return srv


def _strip_wake(text):
    """Normalize STT text; return the query after a wake word, or None."""
    norm = _PUNCT.sub("", text)
    for w in voice_rules.words("wake", WAKE_PREFIXES):
        if norm.startswith(w):
            return norm[len(w) :]
    return None


def _is_sleep_cmd(norm):
    return any(norm == w or norm.endswith(w) for w in voice_rules.words("sleep", SLEEP_WORDS))


# ── 음성 암구호 인증 (WBS 3.8.2 · FR-10.2) ─────────────────────────────────
# 사원증(ArUco)을 보여줄 수 없는 방문객이 말로 인증하는 경로다. 이름은 비밀이
# 아니므로 대조 대상이 아니다 — 등록된 **문구**를 대조한다. 문구는 저장소에
# 두지 않고 환경 변수 `MECHDOG_PASSPHRASES`(JSON 리스트)로 넣는다. 예:
#     $env:MECHDOG_PASSPHRASES = '["우리 문구"]'
# 코드 기본값은 공개된 데모 문구 하나다 — 실제 암구호는 반드시 환경 변수로 교체한다.
PASSPHRASES_ENV = "MECHDOG_PASSPHRASES"
DEFAULT_PASSPHRASES = ("메카독 출입 허가",)


def passphrases():
    """등록 암구호 목록 — 환경 변수 `MECHDOG_PASSPHRASES` 의 JSON 리스트.

    ⚠️ **깨진 설정을 코드 기본값으로 되돌리지 않는다.** 되돌리면 관리자가
    `MECHDOG_PASSPHRASES=우리 문구`(JSON 이 아니다) 처럼 넣었을 때
    **저장소에 공개된 데모 문구가 조용히 문을 연다** — 관리자는 바꿨다고
    믿는다. 빈 목록을 돌려 인증 경로 자체를 닫는다: 열린 채 틀리느니
    닫힌 채 막히는 편이 낫다(호출부가 `bool(passphrases())` 로 판단한다).

    ⚠️ **빈 항목은 버린다** — `""` 가 하나 섞이면 포함 대조가 **항상 참**이라
    모든 발화가 인증을 통과한다.
    """
    raw = os.environ.get(PASSPHRASES_ENV, json.dumps(list(DEFAULT_PASSPHRASES), ensure_ascii=False))
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        print(f"[auth] {PASSPHRASES_ENV} 가 JSON 이 아니다 — 음성 인증을 닫는다")
        return []
    if not isinstance(items, list):
        print(f"[auth] {PASSPHRASES_ENV} 가 목록이 아니다 — 음성 인증을 닫는다")
        return []
    return [text for text in (str(p).strip() for p in items) if text]


def match_passphrase(text):
    """정규화한 발화가 등록 암구호를 **포함**하면 참 — 자연 발화는 문구를
    "암구호는 …입니다" 처럼 감싸므로 부분 포함으로 본다. 포함이란 발화자가
    문구를 그대로 말했다는 뜻이며, 이름 대조와 달리 비밀의 소지 증명이 된다."""
    norm = _PUNCT.sub("", text)
    return any(_PUNCT.sub("", p) in norm for p in passphrases())


def badge_verdict(state, escalation):
    """사원증 대기 중에 본 로봇 상태를 "wait" | "announce" | "drop" 으로 옮긴다.

    ⚠️ **`PATROL` 을 목격하는 조건이었고, 그것이 레이스였다.** 인증이
    끝나면 런타임은 `PATROL` 로 내려오지만 **눈앞에 사람이 남아 있으면
    1~2초 만에 `ALERT` 로 다시 올라간다** — 2026-09-22 실기에서 `PATROL`
    구간이 1.7초였고(20:19:32.558→20:19:34.264) 턴마다 한 번 도는 폴링이
    그 창을 놓쳐 확인 발화가 통째로 사라졌다. 창 길이에 기대지 않는다.

    ⚠️ **`AUTH_WAIT` 를 벗어난 것이 곧 성공은 아니다.** `AUTH_FAILED` 도
    나가는 문이고(→ `ALERT` L3), 링크가 끊기면 `FAILSAFE` 로 빠진다.
    실패한 사람에게 «인증되었습니다» 를 말하는 것은 침묵보다 나쁘다.
    """
    if state in ("AUTH_WAIT", None):
        return "wait"
    if escalation == "L3" or state == "FAILSAFE":
        return "drop"
    return "announce"


def route_query(query):
    """정규화된 질의의 처리 경로 — 구체적인 규칙이 넓은 단어 검사보다 먼저다.

    "비상정지" 는 비상 전파가 아니라 정지 명령이고, "비상접수" 는 접수 시나리오다.
    부분일치 단어 검사(EMERGENCY_WORDS·상태 질의)가 먼저 오면 이 둘을 삼켜 버린다.
    """
    norm = query or ""
    if robotlink.match_action(norm) is not None:
        return "action"
    if scenarios.match_trigger(norm) is not None:
        return "scenario"
    if any(w in norm for w in voice_rules.words("emergency", EMERGENCY_WORDS)):
        return "emergency"
    return "unknown"


def load_knowledge():
    """knowledge/*.txt -> [(name, body)] 회사 데이터 조각들."""
    docs = []
    if KNOW_DIR.is_dir():
        for p in sorted(KNOW_DIR.glob("*.txt")):
            body = p.read_text(encoding="utf-8").strip()
            if body:
                docs.append((p.stem, body))
    return docs


def _bigrams(s):
    n = _PUNCT.sub("", s)
    return {n[i : i + 2] for i in range(len(n) - 1)}


def retrieve(docs, query, max_chars=1200):
    """쿼리와 2-gram 겹침이 큰 문서 상위 3개를 이어 붙인다 (데모 규모 경량 RAG)."""
    q = _bigrams(query)
    if not q or not docs:
        return ""

    def score(d):
        name, body = d
        return len(q & _bigrams(body)) + 3 * len(q & _bigrams(name))

    scored = sorted(docs, key=score, reverse=True)
    out, used = [], 0
    for name, body in scored[:3]:
        if score((name, body)) == 0 or used >= max_chars:
            break
        chunk = body[: max_chars - used]
        out.append(f"[{name}]\n{chunk}")
        used += len(chunk)
    return "\n\n".join(out)


def _say(device, piper, text, speed):
    """고정 안내 멘트 재생. 음성 경로의 모든 발화가 여기를 지난다."""
    print(f"[say] {text}")
    if ROBOT_SPEAKER is not None:
        _say_on_robot(piper, text, speed)
        return
    if device is None:  # XIAO 로 듣기만 할 때 — 말하기 장치가 없으면 출력만
        return
    try:
        stream_play(device, synth_piper(piper, text, speed))
    except (OSError, TimeoutError) as e:
        # 모듈이 한 번 쓰기를 거부해도 대화 루프는 살아있어야 한다
        print(f"[audio] say failed: {e}")


def _say_on_robot(piper, text, speed):
    """문장을 TF 카드 트랙으로 바꿔 로봇 MP3 모듈로 튼다 (WBS 4.7.21).

    표에 없는 문장(서버가 돌려준 거부 사유처럼 미리 녹음할 수 없는 것)은 고정 대체
    문장(`unplayable`)으로 튼다 — 고정 문장의 누락은 `tf_tracks.py --check` 가 CI 에서 막는다. 모듈은 재생 끝을 알리지 않으므로, 카드 음원과 같은 모델·속도로
    합성한 길이만큼 기다린다. 그러지 않으면 로봇 마이크(XIAO)가 제 말을 듣는다.
    """
    track = tf_tracks.track_for(text)
    if track is None:
        print(f"[tf] 표에 없는 문장이라 대체 문장으로 튼다: {text!r}")
        text = pick("unplayable")
        track = tf_tracks.track_for(text)
        if track is None:  # 표를 새로 만들지 않은 카드 — 조용히 넘어가지 않는다
            print(f"[tf] 대체 문장도 표에 없다: {text!r}")
            return
    ok, detail = robotlink.play_track(track, ROBOT_SPEAKER)
    if not ok:
        print(f"[tf] 트랙 {track} 재생 요청 실패: {detail}")
        return
    started = time.monotonic()
    # ponytail: 재생 길이를 다시 합성해서 잰다(~0.2 s). 느리면 --build 가 길이를 표에 적게 바꾼다.
    duration = len(synth_piper(piper, text, speed)) / 32000
    time.sleep(max(0.0, started + duration + ROBOT_SPEAKER_TAIL_S - time.monotonic()))


def _pump(device, decoder):
    """Drain pending UART bytes; return parsed packets."""
    data = device.read(min(device.in_waiting, 4096) or 1)
    return decoder.feed(data, now=time.monotonic())


def _wait_phase(device, decoder, phase, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for packet in _pump(device, decoder):
            if (
                packet.message_type == 0x102
                and len(packet.payload) == STATUS.size
                and STATUS.unpack(packet.payload)[1] == phase
            ):
                return True
    raise TimeoutError(f"no status phase {phase} within {timeout}s")


class _Vad:
    """20ms 프레임 에너지 VAD — WonderEcho·XIAO 두 마이크가 같은 판정을 쓴다.

    마이크마다 규칙이 따로 있으면 암구호·웨이크워드가 한쪽에서만 깨진다.
    `first_speech_ms`·`on_speech` 의 계약은 `capture_pcm` docstring 을 따른다.
    """

    def __init__(self, on_speech=None):
        self.on_speech = on_speech
        self.rms_history = []  # per-20 ms-frame RMS
        self.speech_seen = False
        self.first_speech_ms = None
        self.last_speech = 0.0

    def feed(self, frame, now):
        """Score one frame; True once speech was heard and ~1 s of silence followed."""
        import numpy as np

        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return self.ended(now)
        # 말소리 대역(100–4000 Hz) 에너지만 본다. 로봇에 올린 XIAO 의 소음은 대부분
        # 이 대역 밖이다 — 100 Hz 아래 저주파 흔들림(서 있기 76 %)과 4–8 kHz 서보음
        # (보행 47 %). 전 대역으로 재면 이것이 문턱을 끌어올려 1 m 발화를 놓쳤다
        # (4.7.19 ⑤: 서 있기 문턱 411 vs 발화 최댓값 350–650). 파스발 정리로 rms 환산.
        spec = np.fft.rfft(samples)
        freqs = np.fft.rfftfreq(samples.size, 1.0 / 16000)
        band = (freqs >= 100.0) & (freqs <= 4000.0)
        rms = float(np.sqrt(2.0 * np.sum(np.abs(spec[band]) ** 2)) / samples.size)
        self.rms_history.append(rms)
        # 소음 바닥: 발화 전 초기 25프레임(500ms)의 중앙값
        floor = np.median(self.rms_history[:25]) if len(self.rms_history) >= 25 else 80.0
        if rms > max(floor * 3.0, 120.0):
            if not self.speech_seen:
                # **첫 프레임만 찍는다.** 발화가 시작된 시각이 필요하고
                # 끝난 시각은 이미 호출자가 안다.
                self.first_speech_ms = int(time.time() * 1000)
                if self.on_speech is not None:
                    self.on_speech(self.first_speech_ms)
            self.speech_seen, self.last_speech = True, now
        return self.ended(now)

    def ended(self, now):
        return self.speech_seen and now - self.last_speech > 1.0


def capture_pcm(device, decoder, timeout_s=15.0, vad=True, on_speech=None):
    """Microphone capture -> `(pcm, speech_seen, first_speech_ms)`.

    With `vad` (default), streaming frames are energy-scored as they arrive:
    once speech has been heard, ~1 s of trailing silence ends the capture early
    via the 0x106 stop command instead of waiting out the fixed 5 s window.
    The noise floor is measured from the first 500 ms (before speech starts).

    `first_speech_ms` 는 **사람이 말을 시작한 epoch 밀리초**이며, 말이 없었거나
    `vad=False` 면 `None` 이다.

    ⚠️ **이 값이 왜 필요한가** — 이 함수는 최대 15초를 붙잡고, 돌아간 뒤 전사에
    또 수 초가 든다. 그래서 «말한 시각» 과 «판정이 로봇에 도착한 시각» 이 크게
    벌어진다. 인증 창(`AUTH_WAIT`)이 그 사이에 열리면 **창 밖에서 한 말이 암구호
    시도로 세어져** `max_attempts` 를 혼자 소진하고 곧바로 L3 경보가 된다 —
    2026-09-21 실기에서 인증도 하지 않았는데 눈이 빨개진 원인이다. 호스트가
    이것을 걸러 낼 수 있게 시각을 함께 낸다 (`runtime.note_voice_auth`).

    ⚠️ **`time.monotonic()` 이 아니라 `time.time()` 이다.** 런타임과 다른
    프로세스라 단조 시계는 견줄 수 없다. 규약과 같은 epoch 밀리초를 쓴다.

    `on_speech(first_speech_ms)` 를 주면 **말이 시작된 그 프레임에서 한 번** 부른다
    (말이 없으면 부르지 않는다). 위의 지연을 호스트가 *미리* 알아야 하는 쪽에
    쓴다 — 판정을 기다리는 동안 인증 창이 닫히지 않게 (ADR-37).

    ⚠️ **콜백은 막지도 던지지도 말아야 한다.** 여기는 20ms 프레임 루프 안이다.
    HTTP 왕복을 그 자리에서 하면 프레임을 놓쳐 **녹음 자체가 깨진다.** 호출자가
    스레드로 빼고 예외를 삼키는 것이 규약이며, 여기서는 감싸지 않는다 — 감싸면
    그 규약이 지켜지지 않아도 조용히 넘어가 원인을 못 찾는다.
    """
    import av

    for attempt in range(3):
        device.send_command(0x102)
        try:
            _wait_phase(device, decoder, READY, 3)
            device.send_command(0x108)
            _wait_phase(device, decoder, STARTED, 3)
            break
        except TimeoutError:
            if attempt == 2:
                raise
            time.sleep(0.3)
    pcm_decoder = make_decoder()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = bytearray()
    detector = _Vad(on_speech)
    deadline = time.monotonic() + timeout_s
    done = False
    while not done and time.monotonic() < deadline:
        for packet in _pump(device, decoder):
            if (
                packet.message_type == 0x105
                and len(packet.payload) == 43
                and packet.payload[0] == 42
            ):
                for frame in pcm_decoder.decode(av.Packet(packet.payload[1:])):
                    for out in resampler.resample(frame):
                        pcm.extend(bytes(out.planes[0])[: out.samples * 2])
                if vad and detector.feed(bytes(pcm[-640:]), time.monotonic()):
                    device.send_command(0x106)
                    done = True
            elif packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                if STATUS.unpack(packet.payload)[1] == FINISHED:
                    done = True
    if not pcm:
        raise TimeoutError("no audio frames received")
    if device.in_waiting:
        device.read(device.in_waiting)  # 잔여 프레임 폐기
    return bytes(pcm), detector.speech_seen, detector.first_speech_ms


# XIAO 마이크는 쉬지 않고 듣는다. 녹음 시작 때 버퍼를 비워도 방금 낸 안내 멘트의
# 꼬리(스피커 재생 여유 + XIAO 전송 지연)가 첫 프레임들에 섞여 들어온다. 그것이
# 소음 바닥(첫 25프레임)을 끌어올리거나 발화로 잡히지 않게 앞부분을 버린다.
# 스피커(--port)가 없으면 되돌아올 멘트도 없으므로 버리지 않는다 — 발화 첫머리를
# 잃을 이유가 없다.
ECHO_GUARD_S = 0.3


def capture_xiao(mic, timeout_s=15.0, on_speech=None, guard_s=ECHO_GUARD_S):
    """XIAO `:82/audio` capture -> `(pcm, speech_seen, first_speech_ms)` (WBS 4.7.19).

    Same contract as `capture_pcm` (VAD, `first_speech_ms`, `on_speech`).
    The stream is always on, so stale audio is flushed first. If the link
    stalls after speech started, trailing silence is judged by wall clock so
    the turn still ends instead of waiting out `timeout_s`.
    """
    mic.flush()
    detector = _Vad(on_speech)
    pcm = bytearray()
    skip = int(guard_s * 1000) // 20
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        frame = mic.read_frame(timeout=min(0.2, remaining))
        now = time.monotonic()
        if frame is None:
            if detector.ended(now):
                break
            continue
        if skip:
            skip -= 1
            continue
        pcm.extend(frame)
        if detector.feed(frame, now):
            break
    if not pcm:
        raise TimeoutError("no audio from XIAO mic — link down?")
    return bytes(pcm), detector.speech_seen, detector.first_speech_ms


def open_mic(args):
    """Start the XIAO mic reader when `--xiao` is given; None otherwise."""
    if not args.xiao:
        return None
    return XiaoMic(args.xiao, gain=args.xiao_gain).start()


def listen_pcm(device, decoder, mic, timeout_s, on_speech=None):
    """Capture from the configured mic: XIAO when given (ADR-38), else WonderEcho."""
    if mic is not None:
        guard_s = ECHO_GUARD_S if device is not None or ROBOT_SPEAKER is not None else 0.0
        return capture_xiao(mic, timeout_s=timeout_s, on_speech=on_speech, guard_s=guard_s)
    return capture_pcm(device, decoder, timeout_s=timeout_s, on_speech=on_speech)


# Whisper 도메인 바이어스 — 웨이크워드/명령어 어휘를 알려 주면 "메카독"이
# "내카도"·"레카독"으로 깨지는 오청을 크게 줄인다 (합성 음성 종단간 검증에서 확인).
# 어휘 목록만 둔다. 소리가 흐리면 Whisper 가 힌트 문장을 그대로 내놓는데, 끝에
# 문장("메카독 로봇 음성 명령.")을 두었을 때 1 m 녹음이 그 문장이나 "메카독,
# 비상정지." 로 지어내졌다 (4.7.19 ⑤). 문장을 빼자 같은 녹음이 빈 결과가 됐다.
STT_PROMPT = (
    "메카독, 비상정지, 긴급정지, 스톱, 수동모드, 수동제어, 자동모드, 수동해제, "
    "순찰시작, 순찰정지, 순찰멈춰."
)


# 무음 환각 거름 — faster-whisper 가 무음을 그럴듯한 문장으로 채울 때
# no_speech_prob 가 함께 올라간다 (실측: "오늘도 시청해 주셔서 감사합니다." 0.753).
# 값을 기록만 하면 소비처가 없는 표식이라, 여기서 세그먼트를 실제로 버린다.
NO_SPEECH_PROB_MAX = 0.6

# 힌트 지어내기 거름 — 지어낸 세그먼트는 no_speech_prob 가 낮아도(0.47–0.50)
# 평균 로그 확률이 낮다. XIAO 실측: 지어낸 것 -0.79 ~ -0.91, 실제 명령 -0.11 ~ -0.48
# (가장 낮은 것이 "멈춰."), 30 cm 자유 문장 -0.30 ~ -0.36. 명령을 놓치지 않는 쪽에
# 여유를 둔다.
AVG_LOGPROB_MIN = -0.7


def transcribe(model, pcm_bytes):
    import numpy as np

    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(
        audio,
        language="ko",
        beam_size=5,
        vad_filter=True,
        initial_prompt=STT_PROMPT,
    )
    kept, dropped, guessed = [], [], []
    for seg in segments:
        prob = getattr(seg, "no_speech_prob", 0.0) or 0.0
        logprob = getattr(seg, "avg_logprob", 0.0) or 0.0
        if prob >= NO_SPEECH_PROB_MAX:
            dropped.append(seg)
        elif logprob < AVG_LOGPROB_MIN:
            guessed.append(seg)
        else:
            kept.append(seg)
    # 버린 원문은 찍지 않는다 — 인증 대기 중에는 그 발화가 곧 암구호라서
    # 호출부가 원문을 가린다. 여기서 찍으면 그 가림을 우회한다. 수치만 남긴다.
    if dropped:
        print(
            f"[stt] 무음 환각 추정 세그먼트 {len(dropped)}개 버림 (no_speech "
            + ", ".join(f"{getattr(s, 'no_speech_prob', 0.0):.2f}" for s in dropped)
            + ")"
        )
    if guessed:
        print(
            f"[stt] 낮은 확신 세그먼트 {len(guessed)}개 버림 (avg_logprob "
            + ", ".join(f"{s.avg_logprob:.2f}" for s in guessed)
            + ")"
        )
    return " ".join(seg.text.strip() for seg in kept).strip()


def synth_piper(voice, text, length_scale=1.0):
    """Piper (CPU, ~0.2 s/sentence) -> 16 kHz PCM16. Default for the live loop."""
    import numpy as np
    from piper import SynthesisConfig

    syn = SynthesisConfig(length_scale=length_scale) if length_scale != 1.0 else None
    pcm = bytearray()
    for chunk in voice.synthesize(text, syn_config=syn):
        pcm.extend(chunk.audio_int16_bytes)
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    frame = av.AudioFrame.from_ndarray(
        np.frombuffer(bytes(pcm), dtype=np.int16).reshape(1, -1), format="s16", layout="mono"
    )
    frame.sample_rate = voice.config.sample_rate
    out = bytearray()
    for f in resampler.resample(frame):
        out.extend(bytes(f.planes[0])[: f.samples * 2])
    for f in resampler.resample(None):
        out.extend(bytes(f.planes[0])[: f.samples * 2])
    return bytes(out)


def stream_play(device, pcm, pad_s=0.25):
    """PLAY_DATA stream: silence pad masks the amp/DAC power-on pop, then paced."""
    pcm = b"\x00" * int(32000 * pad_s) + pcm
    t0, sent = time.monotonic(), 0
    for offset in range(0, len(pcm), CHUNK):
        payload = pcm[offset : offset + CHUNK]
        if device.write(play_packet(payload)) != 16 + len(payload):
            raise TimeoutError("incomplete PLAY_DATA write")
        sent += len(payload)
        delay = t0 + max(0, sent - PREFILL) / 32000.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    if device.write(end_packet()) != 16:
        raise TimeoutError("incomplete PLAY_END write")


class ScenarioCtx:
    """What a scenario function gets: hardware access without serial ownership.

    The main loop stays the sole owner of the transport; scenarios borrow it
    through these narrow methods so they can say/listen/check the robot but
    never touch UART framing directly.
    """

    def __init__(self, device, decoder, stt, piper, hub, knowledge, args, mic=None):
        self.device, self.decoder, self.stt, self.piper = device, decoder, stt, piper
        self.hub, self.knowledge, self.args, self.mic = hub, knowledge, args, mic

    def say(self, text):
        self.hub.activity = "speaking(scenario)"
        self.hub.event("robot", text)
        print(f"[scenario] {text!r}")
        if self.device is not None or ROBOT_SPEAKER is not None:  # 스피커 없으면 출력만
            _say(self.device, self.piper, text, self.args.speed)

    def listen(self, timeout_s=12.0):
        self.hub.activity = "listening(scenario)"
        try:
            pcm, heard, _ = listen_pcm(self.device, self.decoder, self.mic, timeout_s)
        except TimeoutError:
            return ""
        if not heard:
            return ""
        text = transcribe(self.stt, pcm)
        self.hub.event("user", text)
        print(f"[scenario stt] {text!r}")
        return text

    def retrieve(self, query):
        return retrieve(self.knowledge, query)

    def command(self, action):
        """Whitelisted robot actions only — free text never reaches the robot."""
        ok, err = robotlink.run_action(action, self.args.robot_api)
        self.hub.event("system", f"명령 {action}: {'성공' if ok else err}")
        return ok

    def event(self, role, text):
        self.hub.event(role, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    if voice_rules.RULES_PATH.exists():
        try:
            voice_rules.read(voice_rules.RULES_PATH)
        except (OSError, ValueError) as exc:
            ap.exit(1, f"[voice-rules] {exc}\n")
    ap.add_argument("--port", help="voice module COM port (never the robot's)")
    ap.add_argument(
        "--xiao",
        help="XIAO 마이크로 듣는다 — 보드 IP (예: 192.168.1.102). :82/audio 를 받는다 (4.7.19)",
    )
    ap.add_argument(
        "--xiao-gain",
        type=int,
        default=2,
        choices=range(5),
        help="XIAO 마이크 이득 0..4 (기본 2, 펌웨어 ?gain=)",
    )
    ap.add_argument("--say", help="synthesize this text and play it once, then exit")
    ap.add_argument(
        "--guard-check",
        action="store_true",
        help="신원 질의·청취·명단 대조·응답을 한 번 실행",
    )
    ap.add_argument("--whisper", default="medium")
    ap.add_argument("--baud", type=int, default=0)
    ap.add_argument("--turns", type=int, default=0, help="0 = loop forever")
    ap.add_argument(
        "--speed",
        type=float,
        default=1.2,
        help="piper length_scale — 1.0 원속, 크면 느려짐 (기본 1.2)",
    )
    ap.add_argument(
        "--piper-model", type=Path, default=Path(r"C:\dev\voice\piper\ko_KR-kss-medium.onnx")
    )
    ap.add_argument(
        "--web", type=int, default=0, help="관제 API HTTP 포트 — 0이면 비활성 (예: 8090)"
    )
    ap.add_argument("--robot-id", default="mechadog-01", help="관제웹에 표시할 로봇 식별자")
    ap.add_argument(
        "--robot-api",
        default=robotlink.DEFAULT_BASE,
        help="로봇 관제 API 베이스 (상태 조회·화이트리스트 명령용)",
    )
    ap.add_argument(
        "--robot-speaker",
        action="store_true",
        help="로봇 MP3 모듈(TF 카드 트랙)로 말한다 — --port 스피커 대신 (WBS 4.7.21)",
    )
    ap.add_argument(
        "--log-dir",
        default=str(Path(__file__).with_name("logs")),
        help="일자별 이벤트 저널 디렉터리 — 빈 문자열이면 기록 안 함 (WBS 4.7.12)",
    )
    args = ap.parse_args()
    global ROBOT_SPEAKER
    ROBOT_SPEAKER = args.robot_api if args.robot_speaker else None
    if args.say and not args.port:
        ap.error("--say 는 --port 가 필요함 (말하기는 아직 WonderEcho 스피커뿐)")
    if not (args.port or args.xiao):
        ap.error("--port(WonderEcho) 나 --xiao(XIAO 마이크) 중 하나는 필요함")
    from transcribe_local import gpu_dll_directories

    _gpu_dll_handles = gpu_dll_directories()  # Keep Windows DLL directories alive until exit.

    if args.guard_check:
        if args.say:
            ap.error("--guard-check 는 --say 와 함께 쓸 수 없음")
        from faster_whisper import WhisperModel
        from piper import PiperVoice

        piper = PiperVoice.load(str(args.piper_model))
        stt = WhisperModel(args.whisper, device="cuda", compute_type="float16")
        device = open_transport(args) if args.port else None
        mic = open_mic(args)
        try:
            ctx = ScenarioCtx(
                device, Decoder(max_payload=128), stt, piper, Hub(args.robot_id), [], args, mic
            )
            scenarios.sc_guard(ctx)
        finally:
            if device is not None:
                device.close()
            if mic is not None:
                mic.stop()
        return

    if args.say:
        from piper import PiperVoice

        piper = PiperVoice.load(str(args.piper_model))
        device = open_transport(args)
        try:
            pcm_out = synth_piper(piper, args.say, args.speed)
            print(f"[tts] {len(pcm_out) / 64000:.1f}s audio -> {device.port} @ {device.baud}")
            stream_play(device, pcm_out)
        finally:
            device.close()
        return

    journal = eventlog.EventJournal(args.log_dir) if args.log_dir else None
    hub = Hub(args.robot_id, journal=journal)
    if args.web:
        start_web(hub, args.web)
    if journal is not None:
        print(f"[journal] {journal.dir}/voice-YYYY-MM-DD.jsonl — /report 로 당일 요약")

    knowledge = load_knowledge()
    if knowledge:
        print(f"[kb] {len(knowledge)} docs: {', '.join(n for n, _ in knowledge)}")

    from faster_whisper import WhisperModel
    from piper import PiperVoice

    piper = PiperVoice.load(str(args.piper_model))
    stt = WhisperModel(args.whisper, device="cuda", compute_type="float16")
    device = open_transport(args) if args.port else None
    decoder = Decoder(max_payload=128)
    mic = hub.mic = open_mic(args)
    if device is not None:
        print(f"[link] {device.kind} {device.port} @ {device.baud}")
    if mic is not None:
        print(f"[link] listen {mic.url}" + ("" if device else " · speak: 출력만"))
    if ROBOT_SPEAKER is not None:
        print(f"[link] speak: 로봇 MP3 모듈 (TF 카드 트랙 {len(tf_tracks.load_table())}개)")

    ctx = ScenarioCtx(device, decoder, stt, piper, hub, knowledge, args, mic)

    def tell_robot_listening(at_ms):
        """«말을 받았다» 를 로봇에게 **즉시** 알린다 — 인증 창 마감 유예 (ADR-37).

        녹음(최대 15초)·무음 1초·전사가 직렬로 들기 때문에, 방문객이 창 안에서
        말해도 판정이 도착할 즈음엔 `auth.timeout_s` 30초가 지나 **빨간 경보**가
        되어 있을 수 있다 — 2026-09-22 실기에서 통과 2건이 24초·18초를 썼다
        (여유 6초). 그래서 말이 시작된 시각을 먼저 보낸다.

        ⚠️ **오디오 루프를 막지 않는다.** 이 함수는 20ms 프레임 사이에서 불리므로
        HTTP 왕복을 그 자리에서 하면 프레임을 놓쳐 녹음이 깨진다. 데몬 스레드로
        던지고 잊는다 — 실패하면 유예를 못 받을 뿐 인증 경로는 그대로 돈다.

        ⚠️ **여기서 상태를 묻지 않는다.** `AUTH_WAIT` 인지 아닌지는 **호스트가**
        판정한다 (`runtime.note_voice_listening`). 파이프라인이 미리 걸러 두면
        같은 규칙이 두 곳에 생기고, 창이 열리는 순간과 어긋난다.
        """
        threading.Thread(
            target=robotlink.post_auth_pending,
            args=(at_ms, args.robot_api),
            daemon=True,
        ).start()

    turn = 0
    follow_until = 0.0
    badge_pending = False
    try:
        while args.turns == 0 or turn < args.turns:
            turn += 1
            state, escalation = robotlink.robot_state_level(args.robot_api)
            if badge_pending:
                verdict = badge_verdict(state, escalation)
                if verdict != "wait":
                    badge_pending = False
                if verdict == "announce":
                    hub.activity = "speaking"
                    _say(device, piper, "인증되었습니다. 다시 순찰을 시작하겠습니다.", args.speed)
            prompt = hub.auth_prompt(state)
            if prompt:
                hub.activity = "speaking"
                _say(device, piper, prompt, args.speed)
            # 로봇 측 사건(검출 확정 등)을 저널에 합친다 — 몇 초에 한 번씩만.
            hub.poll_robot_events(args.robot_api)
            # 단계 경고·시나리오 큐 처리 — 대기 모드에서도 경고는 나간다
            for _, _, item in hub.drain_say():
                if isinstance(item, tuple) and item[0] == "scenario":
                    name = item[1]
                    desc, func = scenarios.SCENARIOS[name]
                    print(f"[scenario] run {name} ({desc})")
                    hub.event("system", f"시나리오 실행: {name}")
                    try:
                        func(ctx)
                    except Exception as e:
                        print(f"[scenario] {name} failed: {e}")
                        hub.event("system", f"시나리오 실패: {name}: {e}")
                    continue
                hub.activity = "speaking(notice)"
                print(f"[notice] {item!r}")
                _say(device, piper, item, args.speed)
            hub.activity = "listening"
            print(f"[turn {turn}] listening (VAD) ...")
            try:
                pcm, speech_seen, spoke_at_ms = listen_pcm(
                    device,
                    decoder,
                    mic,
                    timeout_s=1.5 if badge_pending else 15.0,
                    on_speech=tell_robot_listening if not badge_pending else None,
                )
            except TimeoutError as e:
                print(f"[capture] {e} — skip")
                continue
            if not speech_seen:
                print("[vad] no speech — skip")
                continue
            print(f"[vad] captured {len(pcm) / 32000:.1f}s")
            hub.activity = "thinking"
            text = transcribe(stt, pcm)
            if len(text) < 2:
                continue
            # 인증 대기(AUTH_WAIT) 중의 발화는 암구호 시도다 — 방문객은
            # 웨이크워드를 모르므로 없이도 받는다. 등록 문구가 없으면 이
            # 경로는 열리지 않는다 (빈 목록 대조는 매번 실패를 찍는다).
            auth_wait = (
                hub.mode == "active"
                and bool(passphrases())
                and robotlink.robot_state(args.robot_api) == "AUTH_WAIT"
            )
            if auth_wait and not hub.auth_prompted:
                # 안내 전 시작된 녹음은 암구호 실패로 세지 않는다.
                continue
            # ⚠️ **인증 대기 중에는 인식 원문을 남기지 않는다.** 그 발화가
            # 곧 암구호이고, `/transcript` 는 `0.0.0.0` 에 열려 있어 같은
            # 망의 누구나 `curl` 로 읽는다(CORS 는 브라우저만 막는다).
            # 저장소에 두지 않고 환경 변수로만 넣는 비밀이
            # 여기로 새면 그 방어가 무의미해진다. 판정만 아래에 남긴다.
            if auth_wait:
                print("[stt] <인증 대기 · 원문 가림>")
            else:
                print(f"[stt] {text!r}")
                hub.event("user", text)
            norm = _PUNCT.sub("", text)
            if _is_sleep_cmd(norm):
                if hub.mode == "active":
                    hub.mode = "standby"
                    print("[cmd] sleep mode — '메카독 시작'으로 복귀")
                    hub.event("system", "대기 모드 전환")
                    hub.activity = "speaking"
                    _say(device, piper, "알겠습니다. 대기 모드로 전환합니다.", args.speed)
                continue
            query = _strip_wake(text)
            if hub.mode == "standby":
                if query in voice_rules.words("resume", RESUME_WORDS):
                    hub.mode = "active"
                    print("[cmd] resume")
                    hub.event("system", "대화 재개")
                    hub.activity = "speaking"
                    _say(device, piper, "네, 대화를 다시 시작합니다.", args.speed)
                    follow_until = time.monotonic() + FOLLOW_S
                continue
            if query is None:
                # 후속 대화 창: 직전 답변 직후엔 웨이크워드 없이 받는다.
                # 짧은 잡음(FOLLOW_MIN_CHARS 미만)은 대화로 보지 않는다.
                follow = _PUNCT.sub("", text)
                # ⚠️ **짧은 잡음을 암구호 시도로 세지 않는다.** `AUTH_WAIT`
                # 에서 `AUTH_FAILED` 는 곧바로 `ALERT`(L3) 다(`fsm.py` 전이표).
                # 방문객이 "네?" 라고 되묻거나 옆 대화가 잡히기만 해도 첫
                # 시도에서 경보가 오른다. 실제 재시도 카운터는 없으므로
                # (`fsm.py` 주석은 «2회 실패» 라고 적지만 구현에 없다)
                # 여기서 거른다 — 아무 말도 인증이 못 되면 `AUTH_WAIT` 의
                # 30초 타이머가 맡는다.
                if auth_wait and len(follow) >= FOLLOW_MIN_CHARS:
                    query = follow
                elif (
                    hub.mode == "active"
                    and time.monotonic() < follow_until
                    and len(follow) >= FOLLOW_MIN_CHARS
                ):
                    query = follow
                    print(f"[follow] 웨이크워드 생략 허용: {query!r}")
                else:
                    print("[cmd] no wake word — ignore")
                    continue
            route = route_query(query)
            if route == "emergency":
                print("[cmd] EMERGENCY — logged")
                with Path("emergency_log.txt").open("a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text!r}\n")
                hub.event("system", f"비상: {text}")
                hub.activity = "speaking"
                _say(
                    device,
                    piper,
                    "알겠습니다. 비상 상황을 관제 센터에 전파했습니다. 곧 담당자가 확인할 것입니다.",
                    args.speed,
                )
                follow_until = time.monotonic() + FOLLOW_S
                continue
            # ⚠️ **인증을 건너뛰는 예외는 «비상정지» 하나다.** 화이트리스트
            # 전체를 빼면 인증 대기 중인 로봇 앞의 **미인증자**가 웨이크워드
            # 없이 "순찰 정지" 라고 말하는 것만으로 `POST /api/command/patrol`
            # 이 나간다 — `ACTIONS` 에는 `estop` 말고 `patrol_start`·
            # `patrol_stop`·`manual_on`·`manual_off` 가 같이 들어 있다.
            # 비상정지만 열어 두는 것이 원래 의도였다.
            estop_now = route == "action" and robotlink.match_action(query)[0] == "estop"
            if auth_wait and not estop_now:
                if badge_pending:
                    hub.activity = "speaking"
                    _say(device, piper, "사원증을 카메라에 보여 주세요.", args.speed)
                    continue
                # 인증 대기 중의 발화는 암구호 시도다 — 시나리오 등으로
                # 보내지 않고 등록 문구와 대조해 결과만 돌려보낸다.
                # ⚠️ **대조는 `query` 가 아니라 원문 `text` 로 한다.**
                # `_strip_wake()` 는 «나에게 한 말인가» 를 가르는 라우팅
                # 함수라 웨이크워드를 떼어낸다. 그 값으로 대조하면 등록
                # 문구가 웨이크워드로 시작할 때(출고 기본값
                # "메카독 출입 허가" 가 그렇다) **문구를 정확히 말한 사람이
                # 떨어진다** — 감싸서 말한 사람만 통과하는 역전이 된다.
                # 판정은 사람이 실제로 낸 소리를 봐야 한다.
                ok = match_passphrase(text)
                # ⚠️ **발화 시각을 함께 보낸다.** 위 `auth_wait` 판정은 녹음·
                # 전사가 **끝난 뒤** 상태를 물은 것이라, 창이 열리기 전에 한 말도
                # 여기까지 온다. 그것을 시도로 세면 방문객이 말을 걸기도 전에
                # `max_attempts` 가 소진된다 — 걸러 내는 일은 창이 열린 시각을
                # 아는 런타임이 한다.
                sent_ok, err = robotlink.post_auth_result(
                    ok, args.robot_api, captured_at_ms=spoke_at_ms
                )
                verified = sent_ok and ok and "다시 말해 주세요" not in err
                print(f"[auth] passphrase {'match' if ok else 'mismatch'} → {sent_ok}")
                if not sent_ok:
                    spoken = f"인증 결과를 전달하지 못했습니다. {err}"
                elif ok and "다시 말해 주세요" in err:
                    spoken = err
                elif verified:
                    spoken = "암구호 확인됐습니다. 사원증을 카메라에 보여 주세요."
                    badge_pending = True
                else:
                    spoken = "등록된 암구호와 일치하지 않습니다."
                # 암구호 문구 자체는 저널에 남기지 않는다 — 판정만 기록.
                hub.event("system", f"음성 인증 {'성공' if verified else '실패'}")
                hub.activity = "speaking"
                _say(device, piper, spoken, args.speed)
                follow_until = time.monotonic() + FOLLOW_S
                continue
            if route == "scenario":
                # 규칙 기반 시나리오 트리거 (판정은 결정론적)
                scn_name = scenarios.match_trigger(query)
                desc, func = scenarios.SCENARIOS[scn_name]
                print(f"[scenario] voice trigger {scn_name} ({desc})")
                hub.event("system", f"시나리오 실행: {scn_name}")
                try:
                    func(ctx)
                except Exception as e:
                    print(f"[scenario] {scn_name} failed: {e}")
                    hub.event("system", f"시나리오 실패: {scn_name}: {e}")
                follow_until = time.monotonic() + FOLLOW_S
                continue
            if route == "action":
                # 화이트리스트 명령 — 실행 결과를 그대로 말한다
                _, spoken = robotlink.answer_query(query, args.robot_api)
                print(f"[robotlink] {spoken!r}")
                hub.event("robot", spoken)
                hub.activity = "speaking"
                _say(device, piper, spoken, args.speed)
                follow_until = time.monotonic() + FOLLOW_S
                continue
            # 규칙에 걸리지 않은 발화 — 고정 문구 하나로 답한다(ADR-38).
            # 웨이크워드만 부르면 인사하고 후속 창을 연다.
            if query:
                print(f"[route] 규칙 없음: {query!r}")
                spoken = pick("not_understood")
            else:
                spoken = pick("greeting")
            hub.event("robot", spoken)
            hub.activity = "speaking"
            _say(device, piper, spoken, args.speed)
            follow_until = time.monotonic() + FOLLOW_S
    except KeyboardInterrupt:
        pass
    finally:
        if device is not None and device.is_open:
            device.close()
        if mic is not None:
            mic.stop()


if __name__ == "__main__":
    main()
