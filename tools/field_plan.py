"""Case-linked measurement launcher and notebook. Hardware starts only on explicit request."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ("미측정", "일부 측정", "통과", "실패", "보류")


def catalog() -> dict:
    return json.loads((ROOT / "docs/field_measure_catalog.json").read_text(encoding="utf-8"))


def identities() -> dict[str, str]:
    import yaml

    found = {}
    for path in sorted((ROOT / "config/devices").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("telemetry_device_id"):
            found[path.stem] = data["telemetry_device_id"]
    return found


def read_records(out: Path, device: str) -> list[dict]:
    expected = identities()[device]
    records = []
    paths = set((out / device).glob("*/record.json"))
    if out.name == "기체별_실측" and out.parent.parent.name == "05_실물_측정결과":
        paths.update(out.parent.parent.glob(f"*/기체별_실측/{device}/*/record.json"))
    for path in sorted(paths):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("device") != device or row.get("telemetry_device_id") != expected:
            raise ValueError(f"기체 식별자가 다른 기록: {path}")
        records.append(row)
    return sorted(records, key=lambda r: r["at"])


def record(
    out: Path,
    device: str,
    case_id: str,
    status: str,
    notes: str,
    environment: dict,
    attachments: list[Path],
) -> Path:
    case = next(c for c in catalog()["cases"] if c["id"] == case_id)
    identity = identities()[device]
    if status not in STATUSES or not notes.strip():
        raise ValueError("상태와 실제 관찰 내용을 입력하세요.")
    required = ("operator", "firmware", "host_revision", "conditions")
    if status in ("통과", "실패", "일부 측정") and any(
        not environment.get(key, "").strip() for key in required
    ):
        raise ValueError("측정자·설치 펌웨어·실행 Host 버전·측정 조건을 모두 기록하세요.")
    if status == "통과" and (not attachments or case["phase"] == "제외"):
        raise ValueError("통과에는 증거 파일이 필요합니다. 제외 항목은 통과 처리할 수 없습니다.")
    for path in attachments:
        if not path.is_file():
            raise ValueError(f"증거 파일이 없습니다: {path}")
        if path.suffix.lower() == ".json":
            try:
                incoming = json.loads(path.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError):
                continue  # Other raw formats remain evidence, never auto-accepted.
            if isinstance(incoming, dict) and incoming.get("tool") == "field_measure":
                if incoming.get("device") != device:
                    raise ValueError(
                        "다른 기체의 field_measure 기록입니다. 참고 자료로만 확인하세요."
                    )
                for result in incoming.get("results", []):
                    measured_id = result.get("data", {}).get("telemetry_device_id")
                    if measured_id and measured_id != identity:
                        raise ValueError("수집 기록의 실제 telemetry_device_id가 다릅니다.")
                if status == "통과" and (
                    not incoming.get("results")
                    or not all(
                        result.get("data", {}).get("telemetry_device_id") == identity
                        for result in incoming.get("results", [])
                    )
                ):
                    raise ValueError(
                        "구형 기록의 실제 기체 ID가 없습니다. 일부 측정으로 가져와 신원을 확인하세요."
                    )
    now = datetime.now(UTC)
    folder = out / device / (now.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    evidence = []
    try:
        for index, source in enumerate(attachments):
            target = folder / "evidence" / str(index + 1) / source.name
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            digest = hashlib.file_digest
            with source.open("rb") as src, target.open("rb") as dst:
                original = digest(src, "sha256").hexdigest()
                if original != digest(dst, "sha256").hexdigest():
                    raise ValueError("증거 복사 중 원본이 변경되었습니다. 다시 저장하세요.")
            evidence.append({"file": target.relative_to(folder).as_posix(), "sha256": original})
        payload = {
            "schema": 1,
            "at": now.isoformat(),
            "device": device,
            "telemetry_device_id": identity,
            "case_id": case_id,
            "status": status,
            "notes": notes.strip(),
            "environment": environment,
            "catalog_revision": catalog()["revision"],
            "evidence": evidence,
            "judgement": "operator_recorded_not_automatic_acceptance",
        }
        path = folder / "record.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception:
        # Preserve failed copies for diagnosis; no completed record means no acceptance.
        (folder / "INCOMPLETE.txt").write_text("저장 실패 — 판정에 사용하지 않음", encoding="utf-8")
        raise


def export(out: Path, device: str) -> Path:
    data = catalog()
    latest = {r["case_id"]: r for r in read_records(out, device)}
    lines = [
        f"# {device} 실측 체크리스트",
        "",
        f"기준 Git: `{data['revision']}`",
        "",
        "다른 기체·목업·단위시험 통과를 이 기체의 실측 통과로 옮기지 않는다.",
        "통과는 측정자의 기록이며 WBS 완료 승인이나 기체 설정 반영을 뜻하지 않는다.",
        "",
    ]
    for case in data["cases"]:
        result = latest.get(case["id"], {})
        lines.extend(
            [
                f"## {case['id']} · {case['title']}",
                "",
                f"- 순서/범위: {case['phase']} / {case['scope']}",
                f"- 상태: {result.get('status', '미측정')} (최근 기록일: {result.get('at', '없음')})",
                f"- 요구사항/WBS: {case['requirement']} / {case['wbs']}",
                f"- 준비·선행 조건: {case['needs']}",
                f"- 연계 도구: {case['tool']}",
                "",
                "### 절차",
                "",
                case["steps"],
                "",
                "### 판정 기준",
                "",
                case["criteria"],
                "",
                "### 기존 참고 근거 — 현재 기체의 통과 아님",
                "",
                case["reference"],
                "",
                f"출처: {case['source']}",
                "",
                f"최근 관찰: {result.get('notes', '없음')}",
                "",
            ]
        )
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{device}_실측체크리스트.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def gui(out: Path, device: str) -> None:  # pragma: no cover - GUI 실기 진입점
    import sys
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    sys.path.insert(0, str(ROOT))
    from tools.field_measure_ui import MeasurementWindow
    from tools.field_sessions import PASSIVE, describe, movement_cases, route

    root = tk.Tk()
    root.title("MechaDog 항목별 실측 실행·결과 기록")
    root.geometry("1250x850")
    root.minsize(950, 680)
    data = catalog()
    devices = identities()
    selected_device = tk.StringVar(value=device)
    search = tk.StringVar()
    phase = tk.StringVar(value="전체")
    status = tk.StringVar(value="미측정")
    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    ttk.Label(top, text="기체").pack(side="left")
    box = ttk.Combobox(
        top, textvariable=selected_device, values=list(devices), state="readonly", width=15
    )
    box.pack(side="left", padx=6)
    identity_label = ttk.Label(top)
    identity_label.pack(side="left")
    ttk.Entry(top, textvariable=search, width=24).pack(side="right")
    phases = ["전체", *dict.fromkeys(c["phase"] for c in data["cases"])]
    ttk.Combobox(top, textvariable=phase, values=phases, state="readonly", width=23).pack(
        side="right"
    )
    ttk.Label(
        root,
        text="상태는 가장 최근 시험 기록입니다. 설치 버전·조건이 달라지면 재확인하세요. 증거 가져오기만으로 통과하지 않습니다.",
    ).pack()
    panes = ttk.Panedwindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=8, pady=8)
    left = ttk.Frame(panes)
    right = ttk.Frame(panes)
    panes.add(left, weight=1)
    panes.add(right, weight=2)
    tree = ttk.Treeview(
        left, columns=("status", "phase"), show="tree headings", selectmode="browse"
    )
    tree.heading("#0", text="실측 항목")
    tree.heading("status", text="상태")
    tree.heading("phase", text="순서")
    tree.column("#0", width=270)
    tree.column("status", width=70)
    tree.column("phase", width=90)
    scroll = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True)
    detail = tk.Text(right, wrap="word", height=15)
    detail.pack(fill="both", expand=True)
    env = {}
    for key, label in (
        ("operator", "측정자"),
        ("firmware", "기기에 설치된 FW 버전"),
        ("host_revision", "시험에 사용한 Host 커밋"),
        ("conditions", "바닥·탑재·배터리·카메라·PC·시험 시각"),
    ):
        line = ttk.Frame(right)
        line.pack(fill="x", pady=2)
        ttk.Label(line, text=label, width=32).pack(side="left")
        env[key] = tk.StringVar()
        ttk.Entry(line, textvariable=env[key]).pack(fill="x", expand=True)
    ttk.Label(
        right, text="실제 관찰·측정값·반복 횟수·실패/제외 이유 (과거 자료는 측정일 명시)"
    ).pack(anchor="w")
    notes = tk.Text(right, height=5, wrap="word")
    notes.pack(fill="x")
    files: list[Path] = []
    files_label = ttk.Label(right, text="증거 파일 0개")
    files_label.pack(anchor="w")
    actions = ttk.Frame(right)
    actions.pack(fill="x", pady=6)
    ttk.Combobox(actions, textvariable=status, values=STATUSES, state="readonly", width=10).pack(
        side="left"
    )

    def refresh(*_args):
        identity_label.config(text=devices[selected_device.get()])
        records = read_records(out, selected_device.get())
        latest = {r["case_id"]: r for r in records}
        tree.delete(*tree.get_children())
        for case in data["cases"]:
            if phase.get() not in ("전체", case["phase"]):
                continue
            if search.get().lower() not in json.dumps(case, ensure_ascii=False).lower():
                continue
            tree.insert(
                "",
                "end",
                iid=case["id"],
                text=f"{case['id']} {case['title']}",
                values=(latest.get(case["id"], {}).get("status", "미측정"), case["phase"]),
            )

    def show(*_args):
        selection = tree.selection()
        if not selection:
            return
        case = next(c for c in data["cases"] if c["id"] == selection[0])
        history = [
            r for r in read_records(out, selected_device.get()) if r["case_id"] == case["id"]
        ]
        labels = {
            "title": "항목",
            "scope": "검증 범위",
            "needs": "준비·선행 조건",
            "steps": "측정 절차",
            "criteria": "판정 기준",
            "tool": "연계 도구",
            "reference": "기존 근거 (이 기체의 통과 아님)",
            "source": "출처",
        }
        content = "\n\n".join(f"{label}\n{case[key]}" for key, label in labels.items())
        content += "\n\n이 기체의 기록 이력"
        for row in history:
            content += f"\n{row['at']} {row['status']}\n{row['notes']}"
        detail.config(state="normal")
        detail.delete("1.0", "end")
        detail.insert("end", content)
        detail.config(state="disabled")
        notes.delete("1.0", "end")
        status.set("미측정")
        files.clear()
        files_label.config(text="증거 파일 0개")

    def attach():
        files.extend(
            Path(p)
            for p in filedialog.askopenfilenames(
                title="기존 field_*.json/md·영상·CSV·사진 등 원본 선택"
            )
        )
        files_label.config(text=f"증거 파일 {len(files)}개 — 저장할 때 사본·SHA256 보존")

    def store_result():
        if not tree.selection():
            messagebox.showerror("항목 선택", "왼쪽에서 항목을 선택하세요.")
            return
        if status.get() == "통과" and not messagebox.askyesno(
            "실측 판정 확인",
            "이 기체에서 명시된 기준 전부를 직접 확인했고 증거가 이를 뒷받침합니까?",
        ):
            return
        try:
            path = record(
                out,
                selected_device.get(),
                tree.selection()[0],
                status.get(),
                notes.get("1.0", "end"),
                {k: v.get() for k, v in env.items()},
                files,
            )
            export(out, selected_device.get())
            refresh()
            notes.delete("1.0", "end")
            files.clear()
            files_label.config(text="증거 파일 0개")
            messagebox.showinfo("저장", str(path))
        except (ValueError, OSError) as exc:
            messagebox.showerror("저장 실패", str(exc))

    def export_report():
        try:
            messagebox.showinfo("체크리스트 저장", str(export(out, selected_device.get())))
        except (ValueError, OSError) as exc:
            messagebox.showerror("내보내기 실패", str(exc))

    ttk.Button(actions, text="기존 측정·증거 가져오기", command=attach).pack(side="left", padx=4)
    ttk.Button(actions, text="결과 저장", command=store_result).pack(side="left")
    ttk.Button(actions, text="체크리스트 내보내기", command=export_report).pack(side="right")
    collector = ttk.LabelFrame(right, text="선택 항목 측정", padding=6)
    collector.pack(fill="x")
    host = tk.StringVar()
    ttk.Label(collector, text="로봇 IPv4 (자동 수집/구동 항목)").pack(side="left")
    ttk.Entry(collector, textvariable=host, width=16).pack(side="left", padx=4)
    capability = tk.StringVar(value="왼쪽에서 항목을 선택하세요.")
    ttk.Label(right, textvariable=capability, wraplength=650).pack(anchor="w", pady=5)
    sessions = []

    def start_measurement():
        if any(not session.done for session in sessions):
            messagebox.showerror(
                "측정 진행 중", "진행 중인 측정을 완료하거나 중단한 뒤 시작하세요."
            )
            return
        if not tree.selection():
            messagebox.showerror("항목 선택", "왼쪽에서 측정 항목을 선택하세요.")
            return
        case = next(c for c in data["cases"] if c["id"] == tree.selection()[0])
        actions = route(case)
        if not actions:
            messagebox.showinfo("측정 대기", case["needs"])
            return
        environment = {k: v.get().strip() for k, v in env.items()}
        if not all(environment.values()):
            messagebox.showerror(
                "조건 기록", "측정자·설치 FW·Host 버전·시험 조건을 먼저 입력하세요."
            )
            return
        active = actions != ("guided",) and any(a not in PASSIVE for a in actions)
        if actions != ("guided",):
            import ipaddress

            try:
                ipaddress.IPv4Address(host.get().strip())
            except ValueError:
                messagebox.showerror("주소 확인", "현재 기체의 IPv4 주소를 입력하세요.")
                return
        if active and not messagebox.askyesno(
            "실제 구동/상태 변경 준비 확인",
            describe(case) + "\n\n바닥·주변 공간·독립 중지 수단을 확보했습니까?\n"
            "다른 명령 송신기는 종료해야 합니다. 보행/자세 시험은 RESET_SAFE를 사용합니다.",
        ):
            return

        def completed():
            export(out, selected_device.get())
            refresh()

        session = MeasurementWindow(
            root,
            case,
            selected_device.get(),
            host.get().strip(),
            out,
            environment,
            record,
            completed,
            approved=active,
        )
        sessions.append(session)

    measure_button = ttk.Button(collector, text="선택 항목 측정 시작", command=start_measurement)
    measure_button.pack(side="right")

    def start_movement_batch():
        if any(not session.done for session in sessions):
            messagebox.showerror(
                "측정 진행 중", "진행 중인 측정을 완료하거나 중단한 뒤 시작하세요."
            )
            return
        environment = {k: v.get().strip() for k, v in env.items()}
        if not all(environment.values()):
            messagebox.showerror(
                "조건 기록", "측정자·설치 FW·Host 버전·시험 조건을 먼저 입력하세요."
            )
            return
        import ipaddress

        try:
            ipaddress.IPv4Address(host.get().strip())
        except ValueError:
            messagebox.showerror("주소 확인", "현재 기체의 IPv4 주소를 입력하세요.")
            return
        cases = movement_cases(data["cases"])
        if not cases:
            messagebox.showinfo("측정 대기", "자동 구동 가능한 이동 항목이 없습니다.")
            return
        listing = "\n".join(f"{c['id']} {c['title']}" for c in cases)
        if not messagebox.askyesno(
            "이동 테스트 일괄 실행",
            f"{len(cases)}개 항목을 순서대로 실행합니다:\n\n{listing}\n\n"
            "바닥·주변 공간·독립 중지 수단을 확보했습니까?\n"
            "다른 명령 송신기는 종료해야 합니다. 보행/자세 시험은 RESET_SAFE를 사용합니다.\n"
            "각 항목은 끝나는 즉시 자동 저장되고, 오류 시 일괄 실행이 중단됩니다.",
        ):
            return

        def completed():
            export(out, selected_device.get())
            refresh()

        session = MeasurementWindow(
            root,
            cases[0],
            selected_device.get(),
            host.get().strip(),
            out,
            environment,
            record,
            completed,
            approved=True,
            cases=cases,
        )
        sessions.append(session)

    ttk.Button(collector, text="이동 테스트 일괄 실행", command=start_movement_batch).pack(
        side="right", padx=6
    )

    def update_measurement_button(_event=None):
        if not tree.selection():
            return
        case = next(c for c in data["cases"] if c["id"] == tree.selection()[0])
        actions = route(case)
        capability.set(describe(case))
        measure_button.config(
            text="수동 계측 시작" if actions == ("guided",) else "선택 항목 측정 시작",
            state="normal" if actions else "disabled",
        )

    def close_program():
        running = [session for session in sessions if not session.done]
        if running:
            for session in running:
                session.abort()
            root.after(100, close_program)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_program)
    tree.bind("<<TreeviewSelect>>", show)
    tree.bind("<<TreeviewSelect>>", update_measurement_button, add="+")

    def change_device(_event):
        notes.delete("1.0", "end")
        files.clear()
        files_label.config(text="증거 파일 0개")
        status.set("미측정")
        host.set("")
        for value in env.values():
            value.set("")
        detail.config(state="normal")
        detail.delete("1.0", "end")
        detail.config(state="disabled")
        refresh()

    box.bind("<<ComboboxSelected>>", change_device)
    search.trace_add("write", refresh)
    phase.trace_add("write", refresh)
    refresh()
    root.mainloop()


def main() -> int:  # pragma: no cover - 실기 측정용
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="mechdog-02", choices=list(identities()))
    parser.add_argument("--out", type=Path, default=Path.home() / "MechaDog-measurements")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    if args.export:
        print(export(args.out, args.device))
    else:
        gui(args.out, args.device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
