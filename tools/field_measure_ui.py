"""Nonblocking, case-bound measurement window. Worker never touches Tk widgets."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tools.field_sessions import (
    MeasurementCancelledError,
    MeasurementSession,
    describe,
    route,
    write_session,
)


@dataclass
class Prompt:
    text: str
    answer: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))


class MeasurementWindow:
    def __init__(
        self,
        master,
        case,
        device,
        host,
        out,
        environment,
        save_record,
        refresh,
        approved=False,
        session_factory=MeasurementSession,
        cases=None,
    ):
        self.case, self.device, self.out = case, device, out
        self.cases = list(cases) if cases else [case]
        self.batch = len(self.cases) > 1
        self.active_case = case["id"]
        self.environment, self.save_record, self.refresh = environment, save_record, refresh
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.pending = None
        self.files = []
        self.done = False
        self.closing = False
        self.payload = {
            "schema": 1,
            "case_id": case["id"],
            "device": device,
            "started_at": datetime.now(UTC).isoformat(),
            "environment": environment,
            "execution": describe(case)
            if not self.batch
            else " · ".join(f"{c['id']}" for c in self.cases),
            "steps": [],
            "state": "running",
        }
        if self.batch:
            self.payload["case_ids"] = [c["id"] for c in self.cases]
        self.path = out / "수집원본" / f"{case['id']}_{uuid.uuid4().hex}.json"
        write_session(self.path, self.payload)
        self.window = tk.Toplevel(master)
        title = (
            f"{case['id']} 측정 — {case['title']}"
            if not self.batch
            else f"이동 테스트 일괄 — {len(self.cases)}개 항목"
        )
        self.window.title(title)
        self.window.geometry("870x720")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Label(self.window, text=title, font=("맑은 고딕", 13, "bold")).pack(pady=8)
        ttk.Label(
            self.window,
            text=describe(case)
            if not self.batch
            else "\n".join(f"{c['id']} {c['title']}" for c in self.cases),
            wraplength=820,
        ).pack()
        self.status = tk.StringVar(value="측정 시작 중")
        ttk.Label(self.window, textvariable=self.status, wraplength=820).pack(pady=6)
        self.log = tk.Text(self.window, height=16, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=12)
        self.question = tk.StringVar()
        ttk.Label(self.window, textvariable=self.question, wraplength=820).pack(padx=12, pady=8)
        self.input = tk.StringVar()
        self.entry = ttk.Entry(self.window, textvariable=self.input, width=100)
        self.entry.pack(fill="x", padx=12)
        self.entry.bind("<Return>", lambda _e: self.respond())
        buttons = ttk.Frame(self.window)
        buttons.pack(fill="x", padx=12, pady=10)
        self.next = ttk.Button(
            buttons, text="입력 / 다음 단계", command=self.respond, state="disabled"
        )
        self.next.pack(side="left")
        ttk.Button(buttons, text="영상·사진·계측파일 첨부", command=self.attach).pack(
            side="left", padx=6
        )
        self.stop = ttk.Button(buttons, text="측정 중단", command=self.abort)
        self.stop.pack(side="right")
        self.thread = threading.Thread(
            target=self.work, args=(host, approved, session_factory), daemon=True
        )
        self.thread.start()
        self.window.after(40, self.poll)

    def notify(self, event):
        if isinstance(event, dict) and "result" in event:
            event["result"].setdefault("case_id", self.active_case)
            self.payload["steps"].append(event["result"])
            write_session(self.path, self.payload)
            self.events.put(("log", event["result"]["summary"]))
        else:
            self.events.put(("log", str(event)))

    def ask(self, text):
        request = Prompt(text)
        self.events.put(("prompt", request))
        while not self.cancel.is_set():
            try:
                return request.answer.get(timeout=0.1)
            except queue.Empty:
                continue
        raise MeasurementCancelledError("사용자 중단")

    def guided(self):
        self.notify("이 항목은 수동/외부 장비 시험입니다. 이 화면이 로봇을 자동 조작하지 않습니다.")
        steps = [
            ("준비 조건 확인", self.case["needs"]),
            ("항목별 시험 수행", self.case["steps"]),
            ("실측값·반복수·예상/실제 결과 비교", self.case["criteria"]),
        ]
        for label, text in steps:
            answer = ""
            while not answer.strip():
                answer = self.ask(
                    f"{label}\n{text}\n\n수행 결과/계측값을 입력하세요. 수행할 수 없으면 ‘중단’을 누르세요."
                )
            self.notify(
                {"result": {"item": label, "summary": answer, "verdict": "operator_observation"}}
            )

    def save_case(self, case, mark: int, error: str | None = None):
        """Persist one batch case's record immediately after it finishes."""
        steps = self.payload["steps"][mark:]
        status = "일부 측정" if steps else "보류"
        parts = [] if error is None else [f"실행 오류로 중단: {error}"]
        parts.extend(step["summary"] for step in steps)
        try:
            self.save_record(
                self.out,
                self.device,
                case["id"],
                status,
                "\n".join(parts) or "수집된 단계 없음",
                self.environment,
                [self.path, *self.files],
            )
            self.events.put(("refresh", None))
            self.notify(f"{case['id']} 기록 저장 완료")
        except (ValueError, OSError) as exc:
            self.notify(f"{case['id']} 기록 저장 실패: {exc} (원본: {self.path})")

    def run_batch(self, host, approved, factory):
        for index, case in enumerate(self.cases):
            self.active_case = case["id"]
            self.notify(
                f"[{index + 1}/{len(self.cases)}] {case['id']} {case['title']} — {describe(case)}"
            )
            mark = len(self.payload["steps"])
            try:
                factory(self.device, host, self.ask, self.notify, self.cancel).run(case, approved)
            except Exception as exc:
                self.save_case(case, mark, str(exc))
                self.payload["stopped_at"] = case["id"]
                raise
            self.save_case(case, mark)

    def work(self, host, approved, factory):
        try:
            if self.batch:
                self.run_batch(host, approved, factory)
            elif route(self.case) == ("guided",):
                self.guided()
            else:
                factory(self.device, host, self.ask, self.notify, self.cancel).run(
                    self.case, approved
                )
            self.payload["state"] = "collected"
        except MeasurementCancelledError as exc:
            self.payload.update(state="cancelled", error=str(exc))
        except Exception as exc:
            self.payload.update(state="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            self.payload["finished_at"] = datetime.now(UTC).isoformat()
            try:
                write_session(self.path, self.payload)
            except OSError as exc:
                self.payload.update(state="error", error=f"원본 저장 오류: {exc}")
            self.events.put(("done", None))

    def append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "refresh":
                    self.refresh()
                elif kind == "log":
                    self.status.set(value)
                    self.append(value)
                elif kind == "prompt":
                    self.pending = value
                    self.question.set(value.text)
                    self.input.set("")
                    self.next.config(state="normal")
                    self.entry.focus_set()
                elif kind == "done":
                    self.finish()
                    return
        except queue.Empty:
            pass
        self.window.after(40, self.poll)

    def respond(self):
        if self.pending is not None:
            self.pending.answer.put_nowait(self.input.get())
            self.pending = None
            self.next.config(state="disabled")
            self.question.set("진행 중…")

    def attach(self):
        if self.done:
            return
        chosen = [Path(p) for p in filedialog.askopenfilenames(parent=self.window)]
        self.files.extend(chosen)
        self.append(f"증거 {len(self.files)}개 첨부 예정")

    def abort(self):
        self.cancel.set()
        self.status.set("중단 요청 — 구동 세션의 ESTOP/정리 완료 대기")
        self.next.config(state="disabled")

    def close(self):
        if self.done:
            self.window.destroy()
        else:
            self.closing = True
            self.abort()

    def finish(self):
        self.done = True
        self.next.config(state="disabled")
        self.stop.config(text="닫기", command=self.close)
        state = self.payload["state"]
        if self.batch:
            stopped = self.payload.get("stopped_at")
            text = (
                f"이동 테스트 일괄 {state} — 각 항목은 완료 즉시 자동 저장됨"
                + (f" · {stopped} 에서 중단" if stopped else "")
                + (f" : {self.payload.get('error', '')}" if state == "error" else "")
            )
            self.status.set(text)
            self.question.set(
                "수집 완료는 각 항목 전체의 실물 통과와 다릅니다. 실제 결과와 판정 기준을 확인하세요."
            )
            self.append(f"원본: {self.path}")
            self.refresh()
            if self.closing:
                self.window.destroy()
            return
        text = (
            "측정 완료 · 결과 자동 저장 · 항목 전체 판정은 별도 확인"
            if state == "collected"
            else f"측정 {state}: {self.payload.get('error', '')}"
        )
        summary = "\n".join(s["summary"] for s in self.payload["steps"])
        notes = text + "\n" + summary
        try:
            self.save_record(
                self.out,
                self.device,
                self.case["id"],
                "일부 측정" if self.payload["steps"] else "보류",
                notes,
                self.environment,
                [self.path, *self.files],
            )
            self.refresh()
        except (ValueError, OSError) as exc:
            text += f"\n기록 저장 실패: {exc}. 수집 원본: {self.path}"
            messagebox.showerror("기록 저장 실패", text, parent=self.window)
        self.status.set(text)
        self.question.set(
            "수집 완료는 이 항목 전체의 실물 통과와 다릅니다. 실제 결과와 판정 기준을 확인하세요."
        )
        self.append(f"원본: {self.path}")
        if self.closing:
            self.window.destroy()
