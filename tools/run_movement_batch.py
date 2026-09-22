"""이동 테스트 일괄 실행 — GUI 없이 MeasurementSession을 터미널로 구동한다.

`field_plan.py` 의 "이동 테스트 일괄 실행" 버튼과 동일한 경로:
phase 2 자동 경로 항목을 카탈로그 순서로 실행하고, 각 항목이 끝날 때마다
그 항목의 기록을 즉시 저장한다. 프롬프트는 stdin으로 받는다.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stdin.reconfigure(encoding="utf-8", errors="replace")

from tools import field_measure as fm
from tools import field_plan as plan
from tools import field_sessions as sessions
from tools.field_sessions import write_session

DEVICE = "mechdog-02"
HOST = "192.168.0.18"
OUT = Path(
    "C:/Users/a9800/Desktop/공부/피지컬ai/로봇독 프로젝트/05_실물_측정결과/2026-09-21"
)
ENV = {
    "operator": "사용자(현장)",
    "firmware": "svc-20260915-b",
    "host_revision": "",
    "conditions": "실내 바닥 · 검정 테이프 측정 구역",
}

cancel = threading.Event()
payload: dict = {}
path: Path | None = None
active_case = ""


def notify(event):
    if isinstance(event, dict) and "result" in event:
        event["result"]["case_id"] = active_case
        payload["steps"].append(event["result"])
        write_session(path, payload)
        print(f"    >> {event['result']['summary']}", flush=True)
    else:
        print(f"    .. {event}", flush=True)


def ask(text: str) -> str:
    """파일 브리지 — 질문을 QUESTION.txt로 내고 ANSWER.txt를 기다린다."""
    bridge = OUT / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    answer_path = bridge / "ANSWER.txt"
    answer_path.unlink(missing_ok=True)
    (bridge / "QUESTION.txt").write_text(text, encoding="utf-8")
    print(f"\n[입력대기] {text}", flush=True)
    print(f"           답 -> {answer_path}", flush=True)
    import time

    while not answer_path.exists():
        if cancel.is_set():
            raise sessions.MeasurementCancelledError("사용자 중단")
        time.sleep(0.5)
    answer = answer_path.read_text(encoding="utf-8").strip()
    answer_path.unlink()
    print(f"           입력됨: {answer}", flush=True)
    return answer


def save_case(case: dict, mark: int, error: str | None = None):
    steps = payload["steps"][mark:]
    status = "일부 측정" if steps else "보류"
    parts = [] if error is None else [f"실행 오류로 중단: {error}"]
    parts.extend(s["summary"] for s in steps)
    record_path = plan.record(
        OUT,
        DEVICE,
        case["id"],
        status,
        "\n".join(parts) or "수집된 단계 없음",
        ENV,
        [path],
    )
    print(f"    [저장] {case['id']} 기록 -> {record_path}", flush=True)


def wake_telemetry() -> None:
    """항목 시작 전 세션 오픈을 미리 보내 텔레메트리 peer를 깨운다.

    펌웨어는 검증 명령을 보낸 호스트에게만 텔레메트리를 유니캐스트하고,
    Wi-Fi 재접속 시 peer가 지워진다 — run()은 새 표본을 명령 전에 요구하므로
    차가운 상태나 플랩 직후에는 여기서 먼저 깨워야 한다.
    """
    net = fm.load_config(DEVICE)["network"]
    peer = (HOST, int(net["cmd_port"]))
    cmd = fm.Commander(fm.CommandEncoder(clock=fm.system_clock_ms), period_ms=100)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(3):
            sock.sendto(cmd.open_session().encode(), peer)
            time.sleep(0.2)
        time.sleep(0.4)
    finally:
        sock.close()


def main() -> int:
    global payload, path, active_case
    import subprocess

    ENV["host_revision"] = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    ).stdout.strip() or "unknown"

    cases = sessions.movement_cases(plan.catalog()["cases"])
    only = set(sys.argv[1:])
    if only:
        cases = [c for c in cases if c["id"] in only]
    print(f"일괄 실행 항목 {len(cases)}개: {' · '.join(c['id'] for c in cases)}", flush=True)

    payload = {
        "schema": 1,
        "case_id": cases[0]["id"],
        "case_ids": [c["id"] for c in cases],
        "device": DEVICE,
        "started_at": datetime.now(UTC).isoformat(),
        "environment": ENV,
        "execution": " · ".join(c["id"] for c in cases),
        "steps": [],
        "state": "running",
    }
    path = OUT / "수집원본" / f"BATCH-MOVE_{uuid.uuid4().hex}.json"
    write_session(path, payload)
    print(f"세션 원본: {path}", flush=True)

    try:
        for index, case in enumerate(cases):
            active_case = case["id"]
            print(
                f"\n[{index + 1}/{len(cases)}] {case['id']} {case['title']} :: {sessions.describe(case)}",
                flush=True,
            )
            mark = len(payload["steps"])
            try:
                wake_telemetry()
                sessions.MeasurementSession(DEVICE, HOST, ask, notify, cancel).run(
                    case, approved=True
                )
            except Exception as exc:
                save_case(case, mark, str(exc))
                payload["stopped_at"] = case["id"]
                raise
            save_case(case, mark)
        payload["state"] = "collected"
    except sessions.MeasurementCancelledError as exc:
        payload.update(state="cancelled", error=str(exc))
        print(f"\n[중단] {exc}", flush=True)
    except Exception as exc:
        payload.update(state="error", error=f"{type(exc).__name__}: {exc}")
        print(f"\n[중단] 오류로 일괄 실행 종료: {exc}", flush=True)
    finally:
        payload["finished_at"] = datetime.now(UTC).isoformat()
        write_session(path, payload)
        print(f"\n세션 종료: {payload['state']} · 원본 {path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        cancel.set()
        sys.exit(130)
