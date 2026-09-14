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

import serial  # noqa: F401  (type only; stream_client already imports it)

from stream_client import (
    STATUS, Decoder, open_port, probe_baud, send_command, make_decoder,
)
from play_client import play_packet, end_packet, CHUNK, PREFILL

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
_PUNCT = re.compile(r"[\s,.!?~…:'\"·]+")
KNOW_DIR = Path(__file__).with_name("knowledge")


class Hub:
    """Shared state between the voice loop (serial owner) and the web API.

    Only the main loop touches the serial port; web requests are queued and
    drained between listening turns. `say` items play even in standby —
    관제 공지가 대기 중인 로봇에서도 나가야 하므로.
    """
    def __init__(self, robot_id):
        self.robot_id = robot_id
        self.mode = "active"          # active | standby
        self.activity = "boot"        # listening | thinking | speaking | idle
        self.events = deque(maxlen=200)
        self.say_q = queue.PriorityQueue()
        self._seq = 0
        self.lock = threading.Lock()

    def event(self, role, text):
        self.events.append({
            "ts": time.strftime("%H:%M:%S"),
            "role": role,               # user | robot | admin | system
            "text": text,
        })

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

    def snapshot(self):
        return {
            "robot": self.robot_id,
            "mode": self.mode,
            "activity": self.activity,
            "say_queue": self.say_q.qsize(),
            "events": list(self.events)[-30:],
        }


PANEL_HTML = """<!doctype html><meta charset=utf-8><title>메카독 관제 음성</title>
<style>body{font-family:sans-serif;max-width:560px;margin:2em auto}
#ev{border:1px solid #ccc;height:16em;overflow:auto;padding:.5em;font-size:.9em}
#ev div{margin:.2em 0}.admin{color:#06c}.robot{color:#060}.user{color:#333}
input{width:75%}#st{font-weight:bold}</style>
<h3>메카독 음성 패널</h3>
<div id=st>…</div>
<div style="margin:.6em 0"><input id=t placeholder="로봇에게 말시킬 문장">
<button onclick="say(0)">말하기</button>
<button onclick="say(1)">긴급 방송</button>
<button onclick="fetch('/mode',{method:'POST',body:'{}'}).then(poll)">대기/깨우기</button></div>
<div id=ev></div>
<script>
async function say(u){const t=document.getElementById('t');if(!t.value)return;
 await fetch('/say',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({text:t.value,urgent:!!u})});t.value='';poll()}
async function poll(){const s=await(await fetch('/status')).json();
 document.getElementById('st').textContent=
  `${s.robot} — ${s.mode} / ${s.activity} / 공지대기 ${s.say_queue}`;
 document.getElementById('ev').innerHTML=s.events.map(e=>
  `<div class=${e.role}>[${e.ts}] ${e.role}: ${e.text}</div>`).join('')}
setInterval(poll,2000);poll();
</script>"""


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
            elif self.path == "/transcript":
                _api(self, 200, list(hub.events))
            elif self.path == "/":
                body = PANEL_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
            return norm[len(w):]
    return None


def _is_sleep_cmd(norm):
    return any(norm == w or norm.endswith(w) for w in SLEEP_WORDS)


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
    return {n[i:i + 2] for i in range(len(n) - 1)}


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
        chunk = body[:max_chars - used]
        out.append(f"[{name}]\n{chunk}")
        used += len(chunk)
    return "\n\n".join(out)


def _say(device, piper, text, speed):
    """고정 안내 멘트 재생 — piper 없으면(--tts orpheus) 조용히 건너뜀."""
    if piper is not None:
        stream_play(device, synth_piper(piper, text, speed))


def _pump(device, decoder):
    """Drain pending UART bytes; return parsed packets."""
    data = device.read(min(device.in_waiting, 4096) or 1)
    return decoder.feed(data, now=time.monotonic())


def _wait_phase(device, decoder, phase, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for packet in _pump(device, decoder):
            if packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                if STATUS.unpack(packet.payload)[1] == phase:
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
        send_command(device, 0x102)
        try:
            _wait_phase(device, decoder, READY, 3)
            send_command(device, 0x108)
            _wait_phase(device, decoder, STARTED, 3)
            break
        except TimeoutError:
            if attempt == 2:
                raise
            time.sleep(0.3)
    pcm_decoder = make_decoder()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = bytearray()
    rms_history = []          # per-20 ms-frame RMS
    speech_seen = False
    last_speech = 0.0
    deadline = time.monotonic() + timeout_s
    done = False
    while not done and time.monotonic() < deadline:
        for packet in _pump(device, decoder):
            if (packet.message_type == 0x105 and len(packet.payload) == 43
                    and packet.payload[0] == 42):
                for frame in pcm_decoder.decode(av.Packet(packet.payload[1:])):
                    for out in resampler.resample(frame):
                        pcm.extend(bytes(out.planes[0])[: out.samples * 2])
                samples = np.frombuffer(bytes(pcm[-640:]), dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                rms_history.append(rms)
                if vad:
                    now = time.monotonic()
                    # 소음 바닥: 발화 전 초기 25프레임(500ms)의 중앙값
                    floor = (np.median(rms_history[:25])
                             if len(rms_history) >= 25 else 200.0)
                    if rms > max(floor * 3.0, 300.0):
                        speech_seen, last_speech = True, now
                    if speech_seen and now - last_speech > 1.0:
                        send_command(device, 0x106)
                        done = True
            elif packet.message_type == 0x102 and len(packet.payload) == STATUS.size:
                if STATUS.unpack(packet.payload)[1] == FINISHED:
                    done = True
    if not pcm:
        raise TimeoutError("no audio frames received")
    if device.in_waiting:
        device.read(device.in_waiting)  # 잔여 프레임 폐기
    return bytes(pcm), speech_seen


def transcribe(model, pcm_bytes):
    import numpy as np
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="ko", beam_size=5,
                                 vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


def reply(llm, history, user_text, context=""):
    if context:
        user_text = (f"[참고 자료]\n{context}\n\n"
                     f"[질문] {user_text}\n"
                     "(답변은 참고 자료의 내용만 근거로 한다. 자료에 없으면 없다고 말한다.)")
    history.append({"role": "user", "content": user_text})
    out = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM}] + history,
        max_tokens=96, temperature=0.6)
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
            format="flt", layout="mono")
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
    from piper import SynthesisConfig
    import numpy as np
    syn = SynthesisConfig(length_scale=length_scale) if length_scale != 1.0 else None
    pcm = bytearray()
    for chunk in voice.synthesize(text, syn_config=syn):
        pcm.extend(chunk.audio_int16_bytes)
    import av
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    frame = av.AudioFrame.from_ndarray(
        np.frombuffer(bytes(pcm), dtype=np.int16).reshape(1, -1),
        format="s16", layout="mono")
    frame.sample_rate = voice.config.sample_rate
    out = bytearray()
    for f in resampler.resample(frame):
        out.extend(bytes(f.planes[0])[: f.samples * 2])
    for f in resampler.resample(None):
        out.extend(bytes(f.planes[0])[: f.samples * 2])
    return bytes(out)


def synth_orpheus(text, device="cuda:0"):
    """Orpheus 3B (GPU, ~30 s/sentence) -> 16 kHz PCM16. Higher quality, offline."""
    import synth_prompt_orpheus as orpheus
    import soundfile as sf
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
        payload = pcm[offset:offset + CHUNK]
        if device.write(play_packet(payload)) != 16 + len(payload):
            raise TimeoutError("incomplete PLAY_DATA write")
        sent += len(payload)
        delay = t0 + max(0, sent - PREFILL) / 32000.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    if device.write(end_packet()) != 16:
        raise TimeoutError("incomplete PLAY_END write")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="voice module COM port (never the robot's)")
    ap.add_argument("--model", type=Path, help="GGUF chat model path")
    ap.add_argument("--say", help="synthesize this text and play it once, then exit")
    ap.add_argument("--whisper", default="medium")
    ap.add_argument("--baud", type=int, default=0)
    ap.add_argument("--turns", type=int, default=0, help="0 = loop forever")
    ap.add_argument("--tts", choices=["piper", "orpheus"], default="piper")
    ap.add_argument("--speed", type=float, default=1.2,
                    help="piper length_scale — 1.0 원속, 크면 느려짐 (기본 1.2)")
    ap.add_argument("--piper-model", type=Path,
                    default=Path(r"C:\dev\voice\piper\ko_KR-kss-medium.onnx"))
    ap.add_argument("--dry-llm-only", action="store_true",
                    help="no mic/speaker; type user text, see the reply text")
    ap.add_argument("--web", type=int, default=0,
                    help="관제 API HTTP 포트 — 0이면 비활성 (예: 8090)")
    ap.add_argument("--robot-id", default="mechadog-01",
                    help="관제웹에 표시할 로봇 식별자")
    args = ap.parse_args()

    if args.say:
        from piper import PiperVoice
        piper = PiperVoice.load(str(args.piper_model))
        baud = args.baud or probe_baud(args.port)
        device = open_port(args.port, baud)
        try:
            pcm_out = synth_piper(piper, args.say, args.speed)
            print(f"[tts] {len(pcm_out)/64000:.1f}s audio -> {args.port} @ {baud}")
            stream_play(device, pcm_out)
        finally:
            device.close()
        return

    if not args.model:
        ap.error("--model is required unless --say is used")

    hub = Hub(args.robot_id)
    if args.web:
        start_web(hub, args.web)

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
        baud = args.baud or probe_baud(args.port)
        device = open_port(args.port, baud)
        decoder = Decoder(max_payload=128)
        print(f"[link] {args.port} @ {baud}")

    turn = 0
    try:
        while args.turns == 0 or turn < args.turns:
            turn += 1
            # 관제 공지 큐 처리 — 대기 모드에서도 방송은 나간다
            for _, _, notice in hub.drain_say():
                if args.dry_llm_only:
                    print(f"[notice] {notice!r}")
                    continue
                hub.activity = "speaking(notice)"
                print(f"[notice] {notice!r}")
                _say(device, piper, notice, args.speed)
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
                print(f"[vad] captured {len(pcm)/32000:.1f}s")
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
                    continue
                if query is None:
                    print("[cmd] no wake word — ignore")
                    continue
                if any(w in query for w in EMERGENCY_WORDS):
                    print("[cmd] EMERGENCY — logged")
                    with open("emergency_log.txt", "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text!r}\n")
                    hub.event("system", f"비상: {text}")
                    hub.activity = "speaking"
                    _say(device, piper,
                         "알겠습니다. 비상 상황을 관제 센터에 전파했습니다. 곧 담당자가 확인할 것입니다.",
                         args.speed)
                    continue
                text = query or "불렀어?"
            hub.activity = "thinking"
            t0 = time.time()
            answer = reply(llm, history, text, retrieve(knowledge, text))
            hub.event("robot", answer)
            print(f"[llm] {answer!r} ({time.time()-t0:.1f}s)")
            if args.dry_llm_only:
                continue
            t0 = time.time()
            pcm_out = (synth_piper(piper, answer, args.speed)
                       if args.tts == "piper" else synth_orpheus(answer))
            print(f"[tts] {len(pcm_out)/64000:.1f}s audio ({time.time()-t0:.1f}s synth)")
            hub.activity = "speaking"
            stream_play(device, pcm_out)
    except KeyboardInterrupt:
        pass
    finally:
        if device is not None and device.is_open:
            device.close()


if __name__ == "__main__":
    main()
