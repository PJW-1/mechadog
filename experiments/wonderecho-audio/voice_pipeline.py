"""End-to-end voice loop: module mic -> Whisper STT -> local LLM -> Orpheus TTS -> module speaker.

The WonderEcho module is a peripheral: it owns the microphone and speaker, and
all judgement lives on the PC. This script wires the verified pieces together:

    stream_client   UART <- Speex capture (command 0x108, 5 s window)
    faster-whisper  Korean speech -> text
    llama-cpp       local GGUF chat model -> reply text (Korean, short)
    synth_prompt_orpheus  text -> 24 kHz waveform (SNAC), resampled to 16 kHz
    play_client     16 kHz PCM -> PLAY_DATA packets -> module speaker (firmware 2.1.23+)

Transport note: the serial port (--port, e.g. COM5) is a **temporary**
validation link. The target path is module <-> robot 4-pin I2C <-> ESP32 <->
Wi-Fi (WBS 4.7.9). Everything above the transport boundary — this loop, the
knowledge retrieval, and the --web API — is transport-agnostic: only
capture/stream I/O swaps from UART frames to a Wi-Fi socket. The serial port
is owned exclusively by the main loop for the same reason the future socket
will be: one owner serializes audio frames so they never interleave.

VRAM budget (RTX 5070, 12 GB): whisper medium ~1.5 GB + EXAONE 7.8B Q4 ~5 GB +
Orpheus 3B bf16 ~6.3 GB. Tight; Orpheus loads lazily on first reply. If CUDA
runs out of memory, drop --whisper to `small` or run Orpheus on CPU.

Usage:
    python voice_pipeline.py --port COM8 --model C:\\dev\\voice\\models\\EXAONE-*.gguf
    python voice_pipeline.py --model ... --dry-llm-only     # text in/out, no hardware
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import daily_report
import eventlog
import factorylink
import phrases
import robotlink
import scenarios
import serial  # noqa: F401  (type only; stream_client already imports it)
from play_client import CHUNK, PREFILL, end_packet, play_packet
from stream_client import STATUS, Decoder, make_decoder
from transport import open_transport

SYSTEM = (
    "너는 산업 현장을 순찰하는 네발 경비 로봇 '메카독'이다. "
    "방문자와 한국어로 짧고 명확하게 대화한다. 답변은 한두 문장으로 제한한다. "
    "이모지·마크다운·영어 약어를 쓰지 않는다 — 답변은 그대로 음성으로 읽힌다. "
    "참고 자료가 주어지면 자료 내용만 근거로 답하고, 자료에 없는 내용이나 숫자는 "
    "지어내지 말고 '확인되지 않은 정보'라고 말한다."
)

READY, STARTED, FINISHED = 1, 2, 3

# 음성 명령: "메카독 ..."으로 시작해야만 대답한다.
# "그만/대기"는 대기 모드 전환(프로그램 종료는 Ctrl+C만 — 시연 중 오작동 방지),
# 대기 중에는 "메카독 시작/깨워/일어나"로만 복귀한다.
WAKE_PREFIXES = ("메카독", "메카도", "메카닭", "메카도기")  # STT 변형 흡수
SLEEP_WORDS = ("그만", "꺼져", "대기해", "잠자")
RESUME_WORDS = ("시작", "깨워", "일어나", "켜져")
EMERGENCY_WORDS = ("도와줘", "살려줘", "비상", "응급")
FOLLOW_S = 20.0  # 답변 후 이 시간 안의 발화는 웨이크워드 없이 받는다 (후속 대화)
_PUNCT = re.compile(r"[\s,.!?~…:'\"·]+")
KNOW_DIR = Path(__file__).with_name("knowledge")


class Hub:
    """Shared state between the voice loop (serial owner) and the web API.

    Only the main loop touches the serial port; web requests are queued and
    drained between listening turns. `say` items play even in standby —
    관제 공지가 대기 중인 로봇에서도 나가야 하므로.
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
                for cat, text, custom in phrases.all_lines():
                    cats.setdefault(cat, []).append({"text": text, "custom": custom})
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
            if self.path == "/say":
                text = (req.get("text") or "").strip()
                if not text:
                    _api(self, 400, {"error": "empty text"})
                    return
                hub.enqueue_say(text, req.get("urgent"))
                _api(self, 200, {"queued": hub.say_q.qsize()})
            elif self.path == "/scenario":
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
            elif self.path == "/phrases":
                try:
                    cat = phrases.add_custom(req.get("category") or "", req.get("text") or "")
                except ValueError as e:
                    _api(self, 400, {"error": str(e)})
                    return
                _api(self, 200, {"category": cat, "added": True})
            elif self.path == "/phrases/delete":
                if not phrases.remove_custom(req.get("category") or "", req.get("text") or ""):
                    _api(self, 404, {"error": "추가된 문구만 삭제할 수 있습니다"})
                    return
                _api(self, 200, {"removed": True})
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
    print(f"[web] http://0.0.0.0:{port} — /status /say /mode /transcript")
    return srv


def _strip_wake(text):
    """Normalize STT text; return the query after a wake word, or None."""
    norm = _PUNCT.sub("", text)
    for w in WAKE_PREFIXES:
        if norm.startswith(w):
            return norm[len(w) :]
    return None


def _is_sleep_cmd(norm):
    return any(norm == w or norm.endswith(w) for w in SLEEP_WORDS)


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
    if any(w in norm for w in EMERGENCY_WORDS):
        return "emergency"
    if factorylink.is_factory_query(norm):
        return "factory"
    if robotlink.is_status_query(norm):
        return "status"
    return "llm"


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
    """고정 안내 멘트 재생 — piper 없으면(--tts orpheus) 조용히 건너뜀."""
    if piper is not None:
        try:
            stream_play(device, synth_piper(piper, text, speed))
        except (OSError, TimeoutError) as e:
            # 모듈이 한 번 쓰기를 거부해도 대화 루프는 살아있어야 한다
            print(f"[audio] say failed: {e}")


_MACHINE_WORDS = ("고장", "수리", "설비", "기계", "진단", "안돌", "멈췄", "이상소리")
_MACHINE_NOTICE = (
    "매뉴얼 기준 점검 항목을 안내해 드린 것이며, "
    "실제 고장 판정과 수리는 담당 기술자가 수행해야 합니다."
)


def machine_guard(query, answer):
    """기계·고장 질의의 LLM 답변에 매뉴얼 안내 고지를 붙인다 (결정론적)."""
    norm = _PUNCT.sub("", query or "")
    if any(w in norm for w in _MACHINE_WORDS) and _MACHINE_NOTICE not in answer:
        return answer.rstrip() + " " + _MACHINE_NOTICE
    return answer


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


def capture_pcm(device, decoder, timeout_s=15.0, vad=True):
    """Microphone capture -> decoded 16 kHz PCM16 bytes.

    With `vad` (default), streaming frames are energy-scored as they arrive:
    once speech has been heard, ~1 s of trailing silence ends the capture early
    via the 0x106 stop command instead of waiting out the fixed 5 s window.
    The noise floor is measured from the first 500 ms (before speech starts).
    """
    import av
    import numpy as np

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
    rms_history = []  # per-20 ms-frame RMS
    speech_seen = False
    last_speech = 0.0
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
                samples = np.frombuffer(bytes(pcm[-640:]), dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                rms_history.append(rms)
                if vad:
                    now = time.monotonic()
                    # 소음 바닥: 발화 전 초기 25프레임(500ms)의 중앙값
                    floor = np.median(rms_history[:25]) if len(rms_history) >= 25 else 200.0
                    if rms > max(floor * 3.0, 300.0):
                        speech_seen, last_speech = True, now
                    if speech_seen and now - last_speech > 1.0:
                        device.send_command(0x106)
                        done = True
            elif packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                if STATUS.unpack(packet.payload)[1] == FINISHED:
                    done = True
    if not pcm:
        raise TimeoutError("no audio frames received")
    if device.in_waiting:
        device.read(device.in_waiting)  # 잔여 프레임 폐기
    return bytes(pcm), speech_seen


# Whisper 도메인 바이어스 — 웨이크워드/명령어 어휘를 알려 주면 "메카독"이
# "내카도"·"레카독"으로 깨지는 오청을 크게 줄인다 (합성 음성 종단간 검증에서 확인).
STT_PROMPT = (
    "메카독, 비상정지, 긴급정지, 스톱, 수동모드, 수동제어, 자동모드, 수동해제, "
    "순찰시작, 순찰정지, 순찰멈춰, 배터리 상태, 메카독 로봇 음성 명령."
)


def transcribe(model, pcm_bytes):
    import numpy as np

    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(
        audio, language="ko", beam_size=5, vad_filter=True, initial_prompt=STT_PROMPT
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def reply(llm, history, user_text, context=""):
    if context:
        user_text = (
            f"[참고 자료]\n{context}\n\n"
            f"[질문] {user_text}\n"
            "(답변은 참고 자료의 내용만 근거로 한다. 자료에 없으면 없다고 말한다.)"
        )
    history.append({"role": "user", "content": user_text})
    out = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM}] + history, max_tokens=96, temperature=0.6
    )
    text = out["choices"][0]["message"]["content"].strip()
    history.append({"role": "assistant", "content": text})
    return text


def _to_pcm16k(data, rate):
    """float32 mono -> 16 kHz PCM16 bytes."""
    import numpy as np

    if data.ndim > 1:
        data = data.mean(axis=1)
    if rate != 16000:
        import av

        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(data.astype(np.float32)).reshape(1, -1),
            format="flt",
            layout="mono",
        )
        frame.sample_rate = rate
        out = bytearray()
        for f in resampler.resample(frame):
            out.extend(bytes(f.planes[0]))
        for f in resampler.resample(None):
            out.extend(bytes(f.planes[0]))
        data = np.frombuffer(bytes(out), dtype=np.float32)
    return (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()


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


def synth_orpheus(text, device="cuda:0"):
    """Orpheus 3B (GPU, ~30 s/sentence) -> 16 kHz PCM16. Higher quality, offline."""
    import soundfile as sf
    import synth_prompt_orpheus as orpheus

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        target = tmp.name
    try:
        orpheus.synth_one(target, text, device=device)
        data, rate = sf.read(target, dtype="float32")
    finally:
        Path(target).unlink(missing_ok=True)
    return _to_pcm16k(data, rate)


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

    def __init__(self, device, decoder, stt, piper, hub, knowledge, args):
        self.device, self.decoder, self.stt, self.piper = device, decoder, stt, piper
        self.hub, self.knowledge, self.args = hub, knowledge, args

    def say(self, text):
        self.hub.activity = "speaking(scenario)"
        self.hub.event("robot", text)
        print(f"[scenario] {text!r}")
        if self.device is not None:  # dry-llm 모드에서는 출력만
            _say(self.device, self.piper, text, self.args.speed)

    def listen(self, timeout_s=12.0):
        self.hub.activity = "listening(scenario)"
        try:
            pcm, heard = capture_pcm(self.device, self.decoder, timeout_s=timeout_s)
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

    def robot_status(self):
        return robotlink.fetch_status(self.args.robot_api)

    def command(self, action):
        """Whitelisted robot actions only — free text never reaches the robot."""
        ok, err = robotlink.run_action(action, self.args.robot_api)
        self.hub.event("system", f"명령 {action}: {'성공' if ok else err}")
        return ok

    def event(self, role, text):
        self.hub.event(role, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="voice module COM port (never the robot's)")
    ap.add_argument("--model", type=Path, help="GGUF chat model path")
    ap.add_argument("--say", help="synthesize this text and play it once, then exit")
    ap.add_argument("--whisper", default="medium")
    ap.add_argument("--baud", type=int, default=0)
    ap.add_argument("--turns", type=int, default=0, help="0 = loop forever")
    ap.add_argument("--tts", choices=["piper", "orpheus"], default="piper")
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
        "--dry-llm-only",
        action="store_true",
        help="no mic/speaker; type user text, see the reply text",
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
        "--mes-api",
        default=factorylink.DEFAULT_BASE,
        help="가상 MES API 베이스 (생산·출하·작업지시 조회용)",
    )
    ap.add_argument(
        "--log-dir",
        default=str(Path(__file__).with_name("logs")),
        help="일자별 이벤트 저널 디렉터리 — 빈 문자열이면 기록 안 함 (WBS 4.7.12)",
    )
    args = ap.parse_args()

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

    if not args.model:
        ap.error("--model is required unless --say is used")

    journal = eventlog.EventJournal(args.log_dir) if args.log_dir else None
    hub = Hub(args.robot_id, journal=journal)
    if args.web:
        start_web(hub, args.web)
    if journal is not None:
        print(f"[journal] {journal.dir}/voice-YYYY-MM-DD.jsonl — /report 로 당일 요약")

    from llama_cpp import Llama

    llm = Llama(model_path=str(args.model), n_gpu_layers=-1, n_ctx=4096, verbose=False)
    history = []
    knowledge = load_knowledge()
    if knowledge:
        print(f"[kb] {len(knowledge)} docs: {', '.join(n for n, _ in knowledge)}")

    device, decoder, stt, piper = None, None, None, None
    if args.tts == "piper":
        from piper import PiperVoice

        piper = PiperVoice.load(str(args.piper_model))
    if not args.dry_llm_only:
        from faster_whisper import WhisperModel

        stt = WhisperModel(args.whisper, device="cuda", compute_type="float16")
        device = open_transport(args)
        decoder = Decoder(max_payload=128)
        print(f"[link] {device.kind} {device.port} @ {device.baud}")

    ctx = ScenarioCtx(device, decoder, stt, piper, hub, knowledge, args)
    turn = 0
    follow_until = 0.0
    try:
        while args.turns == 0 or turn < args.turns:
            turn += 1
            # 로봇 측 사건(검출 확정 등)을 저널에 합친다 — 몇 초에 한 번씩만.
            hub.poll_robot_events(args.robot_api)
            # 관제 공지·시나리오 큐 처리 — 대기 모드에서도 방송은 나간다
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
                if args.dry_llm_only:
                    print(f"[notice] {item!r}")
                    continue
                hub.activity = "speaking(notice)"
                print(f"[notice] {item!r}")
                _say(device, piper, item, args.speed)
            if args.dry_llm_only:
                text = input("you> ").strip()
                if not text:
                    continue
            else:
                hub.activity = "listening"
                print(f"[turn {turn}] listening (VAD) ...")
                try:
                    pcm, speech_seen = capture_pcm(device, decoder)
                except TimeoutError as e:
                    print(f"[capture] {e} — skip")
                    continue
                if not speech_seen:
                    print("[vad] no speech — skip")
                    continue
                print(f"[vad] captured {len(pcm) / 32000:.1f}s")
                hub.activity = "thinking"
                text = transcribe(stt, pcm)
                print(f"[stt] {text!r}")
                hub.event("user", text)
                if len(text) < 2:
                    continue
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
                    if query in RESUME_WORDS:
                        hub.mode = "active"
                        print("[cmd] resume")
                        hub.event("system", "대화 재개")
                        hub.activity = "speaking"
                        _say(device, piper, "네, 대화를 다시 시작합니다.", args.speed)
                        follow_until = time.monotonic() + FOLLOW_S
                    continue
                if query is None:
                    # 후속 대화 창: 직전 답변 직후엔 웨이크워드 없이 받는다.
                    # 짧은 잡음(3자 미만)은 대화로 보지 않는다.
                    follow = _PUNCT.sub("", text)
                    if hub.mode == "active" and time.monotonic() < follow_until and len(follow) >= 3:
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
                if route == "scenario":
                    # 규칙 기반 시나리오 트리거 — LLM보다 먼저 잡는다 (판정은 결정론적)
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
                if route == "factory":
                    # 가상 MES 조회 — 숫자·판정은 factorylink 템플릿이 결정한다
                    _, spoken = factorylink.answer_query(query, args.mes_api)
                    print(f"[factorylink] {spoken!r}")
                    hub.event("robot", spoken)
                    hub.activity = "speaking"
                    if not args.dry_llm_only:
                        _say(device, piper, spoken, args.speed)
                    follow_until = time.monotonic() + FOLLOW_S
                    continue
                if route in ("action", "status"):
                    # 화이트리스트 명령 / 로봇 상태 질의 — 실측·실행 결과를 그대로 말한다
                    _, spoken = robotlink.answer_query(query, args.robot_api)
                    print(f"[robotlink] {spoken!r}")
                    hub.event("robot", spoken)
                    hub.activity = "speaking"
                    if not args.dry_llm_only:
                        _say(device, piper, spoken, args.speed)
                    follow_until = time.monotonic() + FOLLOW_S
                    continue
                text = query or "불렀어?"
            hub.activity = "thinking"
            t0 = time.time()
            answer = reply(llm, history, text, retrieve(knowledge, text))
            answer = machine_guard(text, answer)
            hub.event("robot", answer)
            print(f"[llm] {answer!r} ({time.time() - t0:.1f}s)")
            if args.dry_llm_only:
                continue
            t0 = time.time()
            pcm_out = (
                synth_piper(piper, answer, args.speed)
                if args.tts == "piper"
                else synth_orpheus(answer)
            )
            print(f"[tts] {len(pcm_out) / 64000:.1f}s audio ({time.time() - t0:.1f}s synth)")
            hub.activity = "speaking"
            try:
                stream_play(device, pcm_out)
                follow_until = time.monotonic() + FOLLOW_S
            except (OSError, TimeoutError) as e:
                # 모듈 쓰기 거부(일시 행업 등)로 대화 루프 전체가 죽지 않게 한다
                print(f"[audio] play failed: {e}")
                hub.event("system", f"재생 실패: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        if device is not None and device.is_open:
            device.close()


if __name__ == "__main__":
    main()
