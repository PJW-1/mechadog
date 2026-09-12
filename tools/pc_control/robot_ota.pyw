"""Desktop UI for the reviewed-package, pinned TLS OTA client."""

import ipaddress
import sys
import json
import subprocess
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from settings import SETTINGS, require_settings

ROOT = SETTINGS.data_dir
PYTHON = sys.executable
CLIENT = str(Path(__file__).resolve().with_name("ota_worker.py"))



class OtaWindow:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.worker_config = None
        root.title("메크독 Wi-Fi 업데이트")
        root.geometry("720x520")
        root.minsize(680, 490)
        root.configure(bg="#f4f6f8")
        root.option_add("*Font", ("맑은 고딕", 10))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=10)
        frame = tk.Frame(root, bg="#f4f6f8", padx=26, pady=22)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="메크독 Wi-Fi 업데이트",
            font=("맑은 고딕", 20, "bold"),
            bg="#f4f6f8",
            fg="#172b40",
        ).pack(anchor="w")
        tk.Label(
            frame,
            text="로봇을 켜고 PC와 같은 Wi-Fi에 연결해 주세요.",
            bg="#f4f6f8",
            fg="#526071",
        ).pack(anchor="w", pady=(6, 20))
        row = tk.Frame(frame, bg="#f4f6f8")
        row.pack(fill="x")
        tk.Label(row, text="로봇 IP", bg="#f4f6f8").pack(side="left", padx=(0, 10))
        config = SETTINGS.client()
        self.host = tk.StringVar(value=config["host"])
        self.entry = ttk.Entry(row, textvariable=self.host, width=20)
        self.entry.pack(side="left")
        self.check = ttk.Button(
            row, text="연결 확인", command=lambda: self.start("status")
        )
        self.check.pack(side="left", padx=10)
        card = tk.Frame(frame, bg="white", padx=18, pady=18)
        card.pack(fill="x", pady=18)
        self.heading = tk.Label(
            card,
            text="연결 확인을 눌러 주세요",
            bg="white",
            fg="#172b40",
            font=("맑은 고딕", 14, "bold"),
            anchor="w",
        )
        self.heading.pack(fill="x")
        self.detail = tk.Label(
            card,
            text="아직 확인하지 않음",
            bg="white",
            fg="#526071",
            anchor="nw",
            justify="left",
            height=4,
            wraplength=600,
        )
        self.detail.pack(fill="x", pady=(10, 0))
        self.package = tk.StringVar(value=str(SETTINGS.packages[0]) if SETTINGS.packages else "")
        self.choose = ttk.Button(frame, text="업데이트 파일 선택", command=self.pick)
        self.choose.pack(anchor="w")
        self.path_label = tk.Label(
            frame,
            textvariable=self.package,
            bg="#f4f6f8",
            fg="#526071",
            anchor="w",
            wraplength=640,
            justify="left",
        )
        self.path_label.pack(fill="x", pady=(5, 12))
        self.update = ttk.Button(
            frame, text="로봇 업데이트", command=lambda: self.start("update")
        )
        self.update.pack(fill="x")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 8))
        tk.Label(
            frame,
            text="현재 구동 OFF 진단 펌웨어 전용 · 업데이트 중에는 전원을 유지해 주세요.",
            bg="#f4f6f8",
            fg="#526071",
        ).pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.close)

    def pick(self):
        value = filedialog.askopenfilename(
            title="검증된 OTA 패키지 선택",
            initialdir=SETTINGS.data_dir,
            filetypes=[("OTA 패키지", "*.json")],
        )
        if value:
            self.package.set(value)

    def start(self, action):
        if self.process:
            return
        try:
            host = str(ipaddress.IPv4Address(self.host.get().strip()))
        except ValueError:
            messagebox.showerror("IP 확인", "로봇의 IPv4 주소를 입력해 주세요.")
            return
        name = (
            datetime.now().strftime("%Y%m%d-%H%M%S")
            + "-"
            + action
            + "-"
            + uuid4().hex[:8]
        )
        (ROOT / "logs").mkdir(exist_ok=True)
        self.report = ROOT / "logs" / ("wifi-" + name + ".json")
        # Private per-worker configuration allows DHCP changes without publishing credentials.
        config = SETTINGS.client()
        config["host"] = host
        self.worker_config = SETTINGS.data_dir / ("worker-" + name + ".json")
        self.worker_config.write_text(json.dumps(config), encoding="utf-8")
        command = [
            PYTHON,
            CLIENT,
            action,
            "--config",
            str(self.worker_config),
            "--report",
            str(self.report),
        ]
        if action == "update":
            command += ["--package", self.package.get()]
        try:
            self.process = subprocess.Popen(
                command,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.worker_config.unlink(missing_ok=True)
            messagebox.showerror("실행 실패", str(exc))
            return
        self.started = time.monotonic()
        for item in (self.entry, self.check, self.choose, self.update):
            item.state(["disabled"])
        self.heading.configure(
            text="연결 확인 중…" if action == "status" else "무선 업데이트 중…",
            fg="#215ea4",
        )
        self.detail.configure(
            text="파일 검증 → 무선 전송 → 새 부팅 확인 순서로 진행합니다."
            if action == "update"
            else "로봇의 인증서와 실행 상태를 확인합니다."
        )
        self.progress.start(12)
        self.root.after(100, self.poll)

    def poll(self):
        if self.process.poll() is None:
            if time.monotonic() - self.started > 160:
                self.process.kill()
                self.heading.configure(text="시간 초과 — 결과 확인 필요", fg="#995900")
                self.detail.configure(
                    text="전원을 유지하고 잠시 뒤 연결 확인을 눌러 주세요. 성공으로 처리하지 않았습니다."
                )
                self.root.after(100, self.wait_killed)
            else:
                self.root.after(100, self.poll)
            return
        try:
            result = json.loads(self.report.read_text(encoding="utf-8"))
            state = result["state"]
            if state == "FAILED":
                self.heading.configure(
                    text="업데이트 또는 연결 확인 실패", fg="#995900"
                )
                self.detail.configure(
                    text=str(result.get("error", "결과 확인 필요"))[:420]
                )
            else:
                status = result.get("after", result.get("status", {}))
                title = {
                    "STATUS": "로봇 연결 확인",
                    "UPDATED_AND_CONFIRMED": "업데이트 완료",
                    "ALREADY_INSTALLED": "같은 버전이 이미 설치되어 있습니다",
                }.get(state, state)
                self.heading.configure(text=title, fg="#167248")
                self.detail.configure(
                    text=f"버전: {status.get('version')}\n"
                    f"정상 상태: {'확인' if status.get('healthy') else '확인 중'} · "
                    f"부팅 확정: {'완료' if status.get('confirmed') else '대기'}\n"
                    f"구동 OFF · 확인 시각 {datetime.now():%H:%M:%S}"
                )
        except (OSError, ValueError, KeyError):
            self.heading.configure(text="결과를 읽지 못했습니다", fg="#995900")
        self.finish()

    def wait_killed(self):
        if self.process.poll() is None:
            self.root.after(100, self.wait_killed)
        else:
            self.finish()

    def finish(self):
        self.process = None
        self.worker_config.unlink(missing_ok=True)
        self.progress.stop()
        for item in (self.entry, self.check, self.choose, self.update):
            item.state(["!disabled"])

    def close(self):
        if self.process:
            self.detail.configure(
                text="업데이트 결과를 확인한 뒤 창을 닫을 수 있습니다. 전원을 유지해 주세요."
            )
        else:
            self.root.destroy()


if __name__ == "__main__":
    require_settings()
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    OtaWindow(root)
    root.mainloop()
