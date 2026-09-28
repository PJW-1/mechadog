"""WonderEcho 음성 테스트 도구 — 버튼으로 스피커/파이프라인을 확인한다.

로컬 전용 도구. Git에 올리지 않는다.

  python -X utf8 voice_tool.py

버튼:
  스피커 테스트      — COM5에 440Hz 톤을 2초 보내고 진단값을 보여준다.
                       파이프라인이 포트를 잡고 있으면 끈 뒤에 시험한다.
  파이프라인 시작/중지 — voice_pipeline.py를 기동/종료한다.
  상태 새로고침      — 아래 상태가 2초마다 자동 갱신되지만 수동으로도 읽는다.

상태 램프 (공장 장비식):
  파랑 점멸 = 말하는 중(TTS 재생)  파랑 = 듣는 중/대화 가능
  회색 = 대기·꺼짐                빨강 점멸 = 오류(파이프라인 다운/모듈 이탈)
"""

import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
VOICE_API = "http://127.0.0.1:8090"
ROBOT_API = "http://127.0.0.1:8000"
PORT = "COM5"

SITE = r"C:\Users\a9800\AppData\Local\Programs\Python\Python312\Lib\site-packages"
ENV = dict(os.environ)
ENV["PATH"] = (
    rf"{SITE}\torch\lib;{SITE}\nvidia\cublas\bin;{SITE}\nvidia\cuda_nvrtc\bin;" + ENV["PATH"]
)

PIPELINE_CMD = [
    sys.executable,
    "-X",
    "utf8",
    "voice_pipeline.py",
    "--port",
    PORT,
    "--whisper",
    "medium",
    "--web",
    "8090",
    "--robot-id",
    "mechadog-02",
]


def get(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def module_port_state(pipeline_up):
    """COM5가 열리는지로 모듈 존재를 본다 — 파이프라인 점유 중이면 '사용 중'."""
    try:
        sys.path.insert(0, str(HERE))
        import serial

        s = serial.Serial(PORT, 921600, timeout=0.2)
        s.close()
        return "연결됨 (직접 제어 가능)"
    except Exception:
        return "파이프라인이 사용 중" if pipeline_up else "모듈 없음/점검 필요"


def tone_pcm(seconds=2.0, freq=440):
    n = int(16000 * seconds)
    return b"".join(
        struct.pack(
            "<h",
            int(
                12000 * min(1.0, i / 800, (n - i) / 800) * math.sin(2 * math.pi * freq * i / 16000)
            ),
        )
        for i in range(n)
    )


def play_tone(port=PORT, seconds=2.0):
    """직접 PLAY_DATA 스트리밍 — 파이프라인이 꺼져 있어도 테스트 가능."""
    sys.path.insert(0, str(HERE))
    from play_client import CHUNK, PREFILL, end_packet, play_packet
    from protocol import Decoder
    from stream_client import STATUS, open_port, probe_baud, send_command

    pcm = b"\x00" * int(32000 * 0.3) + tone_pcm(seconds)
    device = open_port(port, probe_baud(port))
    decoder = Decoder(max_payload=256)
    diag = {}
    try:
        send_command(device, 0x102)
        deadline = time.monotonic() + 3
        ready = False
        while not ready and time.monotonic() < deadline:
            data = device.read(min(device.in_waiting, 4096) or 1)
            for p in decoder.feed(data, now=time.monotonic()):
                if (
                    p.message_type == 0x102
                    and len(p.payload) == STATUS.size
                    and p.payload[:4] == b"WEC1"
                    and STATUS.unpack(p.payload)[1] == 1
                ):
                    ready = True
        if not ready:
            raise TimeoutError("WEC1 READY 없음 — 펌웨어가 스트리밍을 지원 안 할 수 있음")
        t0, sent = time.monotonic(), 0
        for off in range(0, len(pcm), CHUNK):
            payload = pcm[off : off + CHUNK]
            device.write(play_packet(payload))
            sent += len(payload)
            delay = t0 + max(0, sent - PREFILL) / 32000.0 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        device.write(end_packet())
        deadline = time.monotonic() + len(pcm) / 32000.0 + 4
        while time.monotonic() < deadline and "play_diagnostics" not in diag:
            data = device.read(min(device.in_waiting, 4096) or 1)
            for p in decoder.feed(data, now=time.monotonic()):
                if p.message_type == 0x112 and len(p.payload) in (32, 36, 40):
                    keys = (
                        "bytes_in",
                        "bufs_in",
                        "underrun",
                        "rx_dropped",
                        "output_irqs",
                        "tx_peak",
                        "cfg_rc",
                        "start_rc",
                        "level_peak",
                        "rx_bad",
                    )[: len(p.payload) // 4]
                    diag["play_diagnostics"] = dict(
                        zip(
                            keys, struct.unpack(f"<{len(p.payload) // 4}I", p.payload), strict=False
                        )
                    )
    finally:
        device.close()
    return diag.get("play_diagnostics", {"note": "진단 패킷 미수신"})


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WonderEcho 음성 도구")
        self.proc = None
        self._log_fh = None
        self._stopped_by_user = False
        self.lamp_state = "off"  # off | idle | speaking | error
        self.blink_on = True
        self.build()
        self.refresh()
        self.after(500, self._tick)

    def build(self):
        top = tk.Frame(self)
        top.pack(padx=12, pady=8)
        lamp_col = tk.Frame(top)
        lamp_col.pack(side="left", padx=(0, 12))
        self.lamp = tk.Canvas(lamp_col, width=46, height=46, highlightthickness=0, bg=self["bg"])
        self.lamp.pack()
        self.lamp_id = self.lamp.create_oval(6, 6, 40, 40, fill="#888", outline="#333", width=2)
        self.lamp_txt = tk.Label(lamp_col, text="확인 중", font=("", 9))
        self.lamp_txt.pack()
        btn_col = tk.Frame(top)
        btn_col.pack(side="left", fill="x", expand=True)
        for label, cmd in (
            ("스피커 테스트 (2초 톤)", self.on_tone),
            ("파이프라인 시작/중지", self.on_pipeline),
            ("상태 새로고침", self.refresh),
        ):
            tk.Button(btn_col, text=label, width=26, command=cmd).pack(pady=3)
        self.status = tk.Text(
            self, width=72, height=6, font=("Consolas", 9), bg="#f4f4f4", state="disabled"
        )
        self.status.pack(padx=12, pady=(0, 4))
        self.out = tk.Text(self, width=72, height=14, font=("Consolas", 9))
        self.out.pack(padx=12, pady=(0, 10))

    def log(self, msg):
        self.out.insert("end", msg + "\n")
        self.out.see("end")

    def set_status(self, lines):
        self.status["state"] = "normal"
        self.status.delete("1.0", "end")
        self.status.insert("end", "\n".join(lines))
        self.status["state"] = "disabled"

    # ── 상태 수집·램프 ──────────────────────────────────────────────────

    def collect(self):
        """백그라운드 스레드에서 호출 — 각 API를 읽어 상태 dict를 만든다."""
        v = get(VOICE_API + "/status")
        r = get(ROBOT_API + "/api/telemetry")
        return {"voice": v, "robot": r}

    def refresh(self):
        def work():
            s = self.collect()
            port = module_port_state("error" not in s["voice"])
            self.after(0, lambda: self.apply_status(s, port, verbose=True))

        threading.Thread(target=work, daemon=True).start()

    def apply_status(self, s, port, verbose=False):
        v, r = s["voice"], s["robot"]
        lines = []
        voice_up = "error" not in v
        if voice_up:
            lines.append(
                f"[음성 :8090] {v.get('mode')} / {v.get('activity')} / 큐 {v.get('say_queue')}"
            )
        else:
            lines.append("[음성 :8090] 꺼져 있음")
        if "error" in r:
            lines.append("[런타임 :8000] 꺼져 있음 (로봇 명령만 불가)")
        else:
            t = r.get("telemetry") or {}
            lines.append(
                f"[런타임 :8000] {r.get('device_id')} {r.get('state')} batt={t.get('batt_v')}V"
            )
        lines.append(f"[모듈 {PORT}] {port}")
        self.set_status(lines)

        # 램프 판정 — '꺼짐'과 '오류'를 구분한다:
        #   오류 = 모듈 이탈, 또는 이 도구가 띄운 파이프라인의 비정상 종료
        #   꺼짐 = 파이프라인을 아직 안 켠/정상적으로 끈 상태
        ev = (v.get("events") or [])[-1] if voice_up else None
        module_missing = port == "모듈 없음/점검 필요"
        crashed = (
            self.proc is not None and self.proc.poll() is not None and not self._stopped_by_user
        )
        act = v.get("activity") if voice_up else None
        if module_missing or crashed:
            self.lamp_state = "error"
        elif not voice_up:
            self.lamp_state = "off"
        elif act == "speaking":
            self.lamp_state = "speaking"
        else:
            self.lamp_state = "idle"
        if verbose and ev:
            self.log(f"  최근 이벤트: {ev}")

    def _tick(self):
        """0.5초마다 램프 점멸, 2초마다 상태 자동 갱신."""
        self._n = getattr(self, "_n", 0) + 1
        colors = {"off": "#888", "idle": "#2266ff", "speaking": "#2266ff", "error": "#ff2222"}
        blink_states = ("speaking", "error")
        on = (self._n % 2 == 0) if self.lamp_state in blink_states else True
        self.lamp.itemconfig(self.lamp_id, fill=colors[self.lamp_state] if on else "#f4f4f4")
        labels = {"off": "꺼짐", "idle": "듣는 중", "speaking": "말하는 중", "error": "오류"}
        self.lamp_txt.config(text=labels[self.lamp_state])
        if self._n % 4 == 0:  # 2초마다 자동 갱신 (조용히 — 로그 안 남김)

            def work():
                s = self.collect()
                port = module_port_state("error" not in s["voice"])
                self.after(0, lambda: self.apply_status(s, port))

            threading.Thread(target=work, daemon=True).start()
        self.after(500, self._tick)

    # ── 버튼 ────────────────────────────────────────────────────────────

    def on_tone(self):
        def work():
            # 임의 문장 방송(/say)은 폐기했다(ADR-38) — 파이프라인이 COM5를 잡고
            # 있으면 톤을 보낼 수 없으니 끄고 다시 시험하게 한다.
            if "error" not in get(VOICE_API + "/status", timeout=1):
                self.log("  파이프라인이 COM5를 사용 중입니다. 파이프라인을 끈 뒤 다시 누르세요.")
                return
            self.log("... 스피커 테스트: COM5에 2초 톤 전송 중")
            try:
                diag = play_tone()
            except PermissionError:
                self.log("  실패: COM5 접근 거부 — 다른 프로그램이 포트를 잡고 있습니다.")
                return
            except Exception as e:
                self.log(f"  실패: {e}")
                return
            self.log(f"  진단: {diag}")
            if diag.get("tx_peak"):
                self.log(
                    "  → 디지털 경로 정상. 스피커에서 소리가 안 나면 "
                    "앰프 enable/아날로그 출력 문제입니다."
                )

        threading.Thread(target=work, daemon=True).start()

    def on_pipeline(self):
        if self.proc and self.proc.poll() is None:
            self._stopped_by_user = True
            self.proc.terminate()
            self.proc = None
            self._log_fh.close()
            self.log("... 파이프라인 종료 요청")
            return
        self._stopped_by_user = False
        self.log("... 파이프라인 기동 (Whisper·Piper 로딩 ~30초)")
        # 자식 프로세스 수명 동안 열어 두는 stdout 수신처 — 컨텍스트 매니저 불가
        self._log_fh = (LOG_DIR / "pipeline_stdout.log").open("ab")  # noqa: SIM115
        self.proc = subprocess.Popen(
            PIPELINE_CMD, cwd=str(HERE), env=ENV, stdout=self._log_fh, stderr=subprocess.STDOUT
        )
        self.log(f"  PID {self.proc.pid} — 로그: logs/pipeline_stdout.log")


if __name__ == "__main__":
    LOG_DIR.mkdir(exist_ok=True)
    App().mainloop()
