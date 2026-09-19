"""XIAO 스트림으로 PPE 판정을 눈으로 확인한다 (FR-9 · WBS 3.7.3 선행 점검).

⚠️ **로봇에 명령을 보내지 않는다.** 이 도구는 **읽기 전용**이다. 자세 상승도 경고
방송도 하지 않는다. 판정기(`host/vision/ppe_detector.py`)를 만들기 전에 모델이
실기 프레임에서 어떻게 동작하는지 먼저 보기 위한 것이며, 그래서 모터와 스피커를
건드릴 경로를 아예 만들지 않았다.

    python tools/ppe_live_check.py --xiao-ip 192.168.0.42
    python tools/ppe_live_check.py --images datasets/ppe/raw/ds1/valid   # 카메라 없이
    python tools/ppe_live_check.py --webcam 0 --seconds 600 --segments   # PC 웹캠 + 구간 라벨

판정은 세 갈래다 (PRD FR-9.2.1 · FR-9.3).

    확인불가   머리 미검출 · 몸통 미검출 · 머리가 프레임 상단에 닿음
    위반       머리·몸통 다 보이고 no_helmet 또는 no_vest
    적합       머리·몸통 다 보이고 helmet + vest

⚠️ **확인불가를 먼저 판정한다.** 근거가 없을 때 위반으로 보내면 L3 가 뜨고
관리자 확인으로만 풀린다. 판정 실패도 결과이므로 기록하고 다음 기회에 다시 본다.

⚠️ **사람이 없으면 PPE 모델을 돌리지 않는다** (FR-3.1.1 게이팅). 배경 프레임의
오검출을 원천 차단하는 것이 2단 구조의 실질 이득이다.

⚠️ **`--webcam` 으로 잰 숫자는 FR-9 판정 근거가 아니다.** XIAO 와 센서·ISP·압축이 달라
같은 장면도 다르게 나온다. 통과 판정은 XIAO 실기 프레임으로 한다(OI-13). 카메라를 못
쓰는 동안 모델 거동과 판정 규칙을 먼저 보는 용도다.

⚠️ **`--segments` 의 정확도는 사람 손에 묶여 있다.** 옷을 갈아입는 시간과 버튼을 누르는
시각이 어긋나면 그만큼 정답이 밀린다. 경향으로 읽고, 합격 판정은 라벨을 붙인 촬영본으로
오프라인에서 낸다.
"""

from __future__ import annotations

import argparse
import collections
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.common.config import load_config  # noqa: E402
from host.vision.coco_labels import COCO_CLASSES  # noqa: E402
from host.vision.detector import Detection, Detector  # noqa: E402
from host.vision.stream_client import StreamReader, decode_jpeg  # noqa: E402

STATE_OK = "적합"
STATE_VIOLATION = "위반"
STATE_UNKNOWN = "확인불가"

#: 화면에 그릴 색 (BGR).
COLORS = {
    "helmet": (0, 200, 0),
    "no_helmet": (0, 0, 255),
    "vest": (200, 200, 0),
    "no_vest": (255, 0, 255),
    "person": (200, 200, 200),
}
STATE_COLOR = {STATE_OK: (0, 200, 0), STATE_VIOLATION: (0, 0, 255), STATE_UNKNOWN: (0, 200, 255)}


#: 브라우저로 볼 때 쓰는 경계 문자열.
WEB_BOUNDARY = "mechdog-ppe-frame"

#: 촬영 절차의 여덟 구간. `--segments` 를 줬을 때만 쓴다 (2026-09-19 · PC 웹캠 점검).
#:
#: ⚠️ **오알람을 세려면 정답이 필요하다.** 화면이 «위반» 이라고 할 때 그것이 맞는지는
#: 그 순간 사람이 무엇을 입고 있었는지를 알아야 판정할 수 있다. 도구는 알 수 없으므로
#: 사람이 버튼으로 알려 준다. 안 누르면 그 구간의 판정은 집계에서 빠진다 — 모르는 것을
#: 맞혔다고 세지 않는다.
SEGMENTS: list[dict[str, str]] = [
    {"key": "h100-all", "height": "100cm 수평", "wear": "전부 착용", "expected": STATE_OK},
    {
        "key": "h100-nohelmet",
        "height": "100cm 수평",
        "wear": "안전모 미착용",
        "expected": STATE_VIOLATION,
    },
    {
        "key": "h100-novest",
        "height": "100cm 수평",
        "wear": "조끼 미착용",
        "expected": STATE_VIOLATION,
    },
    {
        "key": "h100-none",
        "height": "100cm 수평",
        "wear": "둘 다 미착용",
        "expected": STATE_VIOLATION,
    },
    {"key": "h30-all", "height": "30cm 대각", "wear": "전부 착용", "expected": STATE_OK},
    {
        "key": "h30-nohelmet",
        "height": "30cm 대각",
        "wear": "안전모 미착용",
        "expected": STATE_VIOLATION,
    },
    {
        "key": "h30-novest",
        "height": "30cm 대각",
        "wear": "조끼 미착용",
        "expected": STATE_VIOLATION,
    },
    {"key": "h30-none", "height": "30cm 대각", "wear": "둘 다 미착용", "expected": STATE_VIOLATION},
]
SEGMENT_BY_KEY = {s["key"]: s for s in SEGMENTS}

#: 한 구간 안에서 몸을 360도 돌리며 90도마다 머무는 시간. 네 방향이면 1분이다.
ORIENT_STEP_S = 15
ORIENTATIONS = ["정면", "우측", "후면", "좌측"]
SEGMENT_TARGET_S = ORIENT_STEP_S * len(ORIENTATIONS)


class SegmentLog:
    """사람이 고른 구간을 정답으로 삼아 판정을 모은다.

    방향은 버튼을 따로 두지 않고 **구간 시작 후 경과 시간으로** 나눈다. 15초마다
    누르게 하면 그 손동작이 화면을 가리고 자세도 흐트러지기 때문이다. 늦게 돌면 그
    몫은 앞 방향에 섞이므로, 방향별 수치는 경향으로만 읽는다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: str | None = None
        self.started: float | None = None
        self.stats: dict[str, dict[str, Any]] = {}

    def _slot(self, key: str) -> dict[str, Any]:
        return self.stats.setdefault(
            key,
            {
                "verdicts": {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0},
                "orientations": {
                    name: {STATE_VIOLATION: 0, STATE_OK: 0, STATE_UNKNOWN: 0}
                    for name in ORIENTATIONS
                },
                "frames": 0,
                "seconds": 0.0,
            },
        )

    def select(self, key: str | None) -> str:
        """구간을 바꾼다. 이전 구간의 관측 시간을 닫고 새 구간을 연다."""
        now = time.monotonic()
        with self._lock:
            if self.current and self.started:
                slot = self._slot(self.current)
                slot["seconds"] = round(slot["seconds"] + (now - self.started), 1)
            self.current = key
            self.started = now if key else None
        if not key:
            return "구간 해제"
        spec = SEGMENT_BY_KEY[key]
        return f"{spec['height']} · {spec['wear']}"

    def facing(self) -> str | None:
        with self._lock:
            if not self.started:
                return None
            index = int((time.monotonic() - self.started) // ORIENT_STEP_S)
        return ORIENTATIONS[min(index, len(ORIENTATIONS) - 1)]

    def observe(self, states: list[str]) -> None:
        """한 프레임의 판정들을 지금 구간에 더한다."""
        facing = self.facing()
        with self._lock:
            if not self.current:
                return
            slot = self._slot(self.current)
            slot["frames"] += 1
            for state in states:
                slot["verdicts"][state] = slot["verdicts"].get(state, 0) + 1
                if facing:
                    bucket = slot["orientations"][facing]
                    bucket[state] = bucket.get(state, 0) + 1

    def status(self) -> dict[str, Any]:
        """브라우저가 0.5초마다 읽어 가는 현재 상태."""
        facing = self.facing()
        with self._lock:
            spec = SEGMENT_BY_KEY.get(self.current or "")
            elapsed = int(time.monotonic() - self.started) if self.started else None
        return {
            "key": self.current,
            "label": f"{spec['height']} · {spec['wear']}" if spec else None,
            "expected": spec["expected"] if spec else None,
            "elapsed_s": elapsed,
            "target_s": SEGMENT_TARGET_S,
            "facing": facing,
            "facing_left_s": (ORIENT_STEP_S - elapsed % ORIENT_STEP_S)
            if elapsed is not None
            else None,
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            out = {
                key: {
                    **value,
                    "verdicts": dict(value["verdicts"]),
                    "orientations": {n: dict(c) for n, c in value["orientations"].items()},
                }
                for key, value in self.stats.items()
            }
            if self.current and self.started:
                live = out.setdefault(self.current, {"seconds": 0.0})
                live["seconds"] = round(live.get("seconds", 0.0) + (now - self.started), 1)
        return out


class FrameRelay:
    """판정을 그린 프레임을 브라우저로 중계한다.

    ⚠️ **XIAO 스트림은 한 클라이언트만 받는다.** 브라우저로 카메라에 직접 붙으면
    이 도구가 한 장도 못 받고, 반대도 마찬가지다 — 2026-09-14 실측에서 나중에 붙은
    쪽은 두 번 모두 0프레임 · TimeoutError 였다. 그래서 **카메라에는 이 도구만
    붙고**, 사람은 여기서 다시 내보내는 화면을 본다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._seq = 0

    def publish(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._seq += 1

    def latest(self) -> tuple[bytes | None, int]:
        with self._lock:
            return self._jpeg, self._seq


#: `--segments` 일 때만 화면에 붙는 조각. 버튼이 곧 «그 시간의 정답» 이다.
SEGMENT_PANEL = (
    "<div id='seg' style='display:grid;grid-template-columns:1fr 1fr;gap:6px;max-width:520px'></div>"
    "<div id='now' style='font-size:16px;font-weight:700'></div>"
    "<button onclick=\"fetch('/segment?key=',{method:'POST'})\">구간 해제</button>"
    "<script>"
    "let drawn=false;"
    "async function poll(){try{const s=await(await fetch('/status')).json();"
    "if(!drawn){drawn=true;document.getElementById('seg').innerHTML=s.segments.map(x=>"
    "`<button data-k='${x.key}' onclick=\"fetch('/segment?key=${x.key}',{method:'POST'})\">"
    "${x.height}<br><small>${x.wear} → ${x.expected}</small></button>`).join('');}"
    "document.querySelectorAll('#seg button').forEach(b=>"
    "b.style.outline=b.dataset.k===s.key?'2px solid #6cf':'none');"
    "document.getElementById('now').textContent=s.key?"
    "`${s.label} — ${s.elapsed_s}/${s.target_s}초 · 지금 ${s.facing} · ${s.facing_left_s}초 뒤 90도 회전`:"
    "'구간 미선택 — 이 동안의 판정은 집계에서 빠진다';"
    "}catch(e){}}"
    "setInterval(poll,500);poll();"
    "</script>"
)


def start_web(
    relay: FrameRelay, host: str, port: int, segments: SegmentLog | None = None
) -> ThreadingHTTPServer:
    page = (
        "<!doctype html><meta charset='utf-8'><title>PPE 판정 화면</title>"
        "<style>body{margin:0;background:#111;color:#eee;font:14px system-ui;"
        "display:flex;flex-direction:column;align-items:center;gap:8px;padding:12px}"
        "img{max-width:100%;border:1px solid #333}"
        "button{background:#222;color:#eee;border:1px solid #444;border-radius:6px;"
        "padding:6px 10px;cursor:pointer;text-align:left}</style>"
        "<h3>PPE 판정 — 초록 적합 · 빨강 위반 · 주황 확인불가</h3>"
        "<img src='/stream'>" + (SEGMENT_PANEL if segments is not None else "")
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args) -> None:  # 콘솔을 판정 로그만 남기게 둔다
            pass

        def do_POST(self) -> None:
            """구간 선택. 버튼이 누른 값이 그때부터의 정답이 된다."""
            path, _, query = self.path.partition("?")
            if segments is None or path != "/segment":
                self.send_error(404)
                return
            key = query.removeprefix("key=") or None
            if key is not None and key not in SEGMENT_BY_KEY:
                self.send_error(400)
                return
            print(f"  구간 → {segments.select(key)}")
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            if self.path.startswith("/stream"):
                self._serve_stream()
            elif self.path.startswith("/status") and segments is not None:
                import json

                payload = {**segments.status(), "segments": SEGMENTS}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        def _serve_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={WEB_BOUNDARY}")
            self.end_headers()
            last = -1
            try:
                while True:
                    jpeg, seq = relay.latest()
                    if jpeg is None or seq == last:
                        time.sleep(0.05)
                        continue
                    last = seq
                    self.wfile.write(f"--{WEB_BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # 브라우저가 탭을 닫은 것뿐이다

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    print(f"웹 화면 http://{shown}:{port}/  — 브라우저로 연다")
    return server


@dataclass(frozen=True, slots=True)
class Judgement:
    state: str
    reason: str
    detections: tuple[Detection, ...]


class ViolationWindow:
    """시간 창 안의 위반 히트를 센다 (FR-9.4 · ADR-25).

    ⚠️ **프레임 수가 아니라 시간이다.** 프레임으로 세면 같은 3히트가 추론률에 따라
    다른 시간을 뜻하게 된다 — 카메라가 느려지면 조건이 저절로 느슨해진다.
    """

    def __init__(self, window_ms: int, hits_required: int) -> None:
        self._window = int(window_ms)
        self._required = int(hits_required)
        self._hits: collections.deque[int] = collections.deque()
        self.confirmed = False

    def observe(self, violating: bool, now_ms: int) -> bool:
        """이번 프레임을 넣고 **새로 확정됐는지** 돌려준다."""
        if violating:
            self._hits.append(now_ms)
        while self._hits and now_ms - self._hits[0] > self._window:
            self._hits.popleft()

        if len(self._hits) >= self._required:
            newly = not self.confirmed
            self.confirmed = True
            return newly
        # ⚠️ 진입과 해제를 비대칭으로 둔다 — 창이 완전히 비어야 푼다. 같은 조건으로
        # 풀면 경계에서 떨림이 생겨 경고가 켜졌다 꺼졌다 한다.
        if not self._hits:
            self.confirmed = False
        return False

    @property
    def hits(self) -> int:
        return len(self._hits)


def judge(ppe: list[Detection], head_margin_px: int, use_clip: bool) -> Judgement:
    """PPE 검출 목록 → 세 상태 중 하나."""
    heads = [d for d in ppe if d.label in ("helmet", "no_helmet")]
    torsos = [d for d in ppe if d.label in ("vest", "no_vest")]

    if not heads and not torsos:
        return Judgement(STATE_UNKNOWN, "아무것도 미검출", tuple(ppe))
    if not heads:
        return Judgement(STATE_UNKNOWN, "머리 미검출", tuple(ppe))
    if not torsos:
        return Judgement(STATE_UNKNOWN, "몸통 미검출", tuple(ppe))
    # ⚠️ 머리가 잘린 프레임에서는 안전모를 볼 수 없다 (FR-9.2.1 · DR-15).
    # 실기에서는 여기서 자세 상승으로 넘어간다 — 이 도구는 관찰만 한다.
    if use_clip and min(d.box[1] for d in heads) <= head_margin_px:
        return Judgement(STATE_UNKNOWN, "머리 클리핑", tuple(ppe))

    if any(d.label in ("no_helmet", "no_vest") for d in ppe):
        return Judgement(STATE_VIOLATION, "", tuple(ppe))
    return Judgement(STATE_OK, "", tuple(ppe))


def crop_person(image: np.ndarray, box: tuple[float, float, float, float], pad: float):
    """person bbox 를 여유를 두고 잘라낸다. 자른 좌표 원점을 함께 돌려준다.

    ⚠️ **풀프레임이 아니라 크롭을 넣는다** (MODEL_PLAN 1.6). 입력이 작아져 정확도와
    속도를 함께 얻는 것이 2단 구조의 취지다.
    """
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    cx1 = max(0, int(x1 - dx))
    cy1 = max(0, int(y1 - dy))
    cx2 = min(width, int(x2 + dx))
    cy2 = min(height, int(y2 + dy))
    if cx2 - cx1 < 8 or cy2 - cy1 < 8:
        return None, (0, 0)
    return image[cy1:cy2, cx1:cx2], (cx1, cy1)


def annotate(image, person: Detection, judgement: Judgement, origin: tuple[int, int]):
    import cv2

    ox, oy = origin
    x1, y1, x2, y2 = (int(v) for v in person.box)
    color = STATE_COLOR[judgement.state]
    cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
    text = judgement.state + (f" ({judgement.reason})" if judgement.reason else "")
    cv2.putText(image, text, (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    for d in judgement.detections:
        bx1, by1, bx2, by2 = (int(v) for v in d.box)
        cv2.rectangle(image, (bx1 + ox, by1 + oy), (bx2 + ox, by2 + oy), COLORS[d.label], 1)
        cv2.putText(
            image,
            f"{d.label} {d.score:.2f}",
            (bx1 + ox, min(image.shape[0] - 4, by2 + oy + 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            COLORS[d.label],
            1,
        )


def process(
    frame_image: np.ndarray,
    coco: Detector,
    ppe: Detector,
    *,
    person_label: str,
    pad: float,
    head_margin: int,
    use_clip: bool,
) -> list[tuple[Detection, Judgement, tuple[int, int]]]:
    people = [d for d in coco.detect(frame_image) if d.label == person_label]
    out = []
    for person in people:
        crop, origin = crop_person(frame_image, person.box, pad)
        if crop is None:
            out.append((person, Judgement(STATE_UNKNOWN, "크롭 실패", ()), origin))
            continue
        judged = judge(ppe.detect(crop), head_margin, use_clip)
        out.append((person, judged, origin))
    return out


def wait_for_camera(config, minutes: float) -> bool:
    """카메라가 스스로 준비될 때까지 기다린다.

    ⚠️ **스트림에 먼저 붙지 않는다.** 제어 포트(80)의 상태만 읽는다 — 스트림은
    한 클라이언트만 받으므로, 기다리는 동안 붙어 있으면 정작 확인하려는 순간에
    자리를 차지한 쪽이 된다.

    ⚠️ **`ok` 만 보지 않는다.** 센서 이름이 `UNKNOWN` 이면 보드는 살아 있고
    카메라만 죽은 상태다 — 그대로 시작하면 빈 화면을 보게 된다.
    """
    import json
    import urllib.error
    import urllib.request

    host = config["network"]["xiao_ip"]
    url = f"http://{host}:{int(config['network']['vision_control_port'])}/"
    deadline = time.monotonic() + minutes * 60
    print(f"카메라 대기 — {url} · 최대 {minutes:.0f}분")
    last_note = ""
    while time.monotonic() < deadline:
        note = ""
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
            sensor = str(data.get("sensor", ""))
            if data.get("ok") and sensor and sensor != "UNKNOWN":
                print(
                    f"카메라 준비됨 — sensor={sensor} profile={data.get('profile')} "
                    f"rssi={data.get('rssi')}"
                )
                return True
            note = f"보드는 응답하는데 카메라가 아니다 (sensor={sensor or '없음'})"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            note = "아직 응답 없음 — 전원과 리본을 확인한다"
        if note != last_note:
            print(f"  {note}")
            last_note = note
        time.sleep(3)
    print(f"⚠️ {minutes:.0f}분 동안 카메라가 준비되지 않았다")
    return False


def segment_section(add, snapshot: dict[str, dict[str, Any]]) -> None:
    """구간을 눌러 가며 봤을 때만 쓰는 절. **정답을 알 때만 오판정을 셀 수 있다.**

    비율의 분모는 그 구간의 판정 전부(정상 + 오판정 + 확인불가)이고, «정상판정 비율»
    만 확인불가를 뺀 값이다 — 판정을 내린 것 중 맞힌 비율이다.
    """
    total_right = total_judged = 0
    rows: list[str] = []
    for number, spec in enumerate(SEGMENTS, start=1):
        stats = snapshot.get(spec["key"])
        wrong_state = STATE_VIOLATION if spec["expected"] == STATE_OK else STATE_OK
        title = f"{spec['height']} · {spec['wear']} (정상 = {spec['expected']} 판정)"
        if not stats or not stats.get("frames"):
            rows.append(f"{number}. {title} — 관측 없음")
            continue
        verdicts = stats["verdicts"]
        right = verdicts.get(spec["expected"], 0)
        wrong = verdicts.get(wrong_state, 0)
        unknown = verdicts.get(STATE_UNKNOWN, 0)
        total = right + wrong + unknown
        total_right += right
        total_judged += right + wrong

        def pct(n: int, base: int = total) -> str:
            return f"{n / base:.0%}" if base else "-"

        rows.append(
            f"{number}. {title} — {stats['seconds']}초 · 프레임 {stats['frames']}장 · 판정 {total}건"
        )
        rows.append(f"   - 정상판정 {right}건 · {pct(right)}")
        rows.append(f"   - 오판정({wrong_state}) {wrong}건 · {pct(wrong)}")
        rows.append(f"   - 확인불가 {unknown}건 · {pct(unknown)}")
        rows.append(f"   - 정상판정 비율(확인불가 제외) {pct(right, right + wrong)}")
        for name in ORIENTATIONS:
            counts = stats["orientations"].get(name, {})
            seen = sum(counts.values())
            if not seen:
                continue
            rows.append(
                f"   - 방향 {name} {seen}건 — 정상 {counts.get(spec['expected'], 0)}건 · "
                f"오판정 {counts.get(wrong_state, 0)}건 · 확인불가 {counts.get(STATE_UNKNOWN, 0)}건"
            )

    overall = f"{total_right / total_judged:.0%}" if total_judged else "판정 없음"
    add("")
    add(
        f"## 구간별 결과 — 전체 정상판정 {overall} (정상 {total_right} / 정상+오판정 {total_judged})"
    )
    add("")
    add("사람이 버튼으로 알려 준 구간이 그 시간의 정답이다. 누르지 않은 동안은 빠진다.")
    add("")
    for row in rows:
        add(row)
    add("")
    add("⚠️ **이 수치는 경향이다.** 옷을 갈아입는 시간, 버튼을 누르는 시각, 몸을 돌리는")
    add("시점이 모두 사람 손에 달려 있어 정답과 화면이 수 초씩 어긋난다. 방향은 버튼조차")
    add("없이 경과 시간으로 나눈다. 합격 판정은 라벨을 붙인 촬영본으로 오프라인에서 낸다.")


def write_report(
    path, args, config, window_ms, hits, frames, elapsed, counts, reasons, events, segment_log=None
) -> None:
    """시험 결과를 사람이 읽을 MD 로 남긴다.

    ⚠️ **합·불이 아니라 숫자를 적는다.** 엑셀 WBS 의 «결과·검증 요약» 칸이 요구하는
    것이 그것이고, 나중에 같은 조건에서 다시 쟀을 때 비교할 수 있어야 한다.
    ⚠️ **모델 해시와 설정값을 함께 적는다.** 어떤 가중치로 낸 숫자인지 모르면
    재현할 수 없다 (FR-9.1.2).
    """
    import hashlib

    ppe_cfg = config["vision"]["ppe"]
    model_path = Path(__file__).resolve().parents[1] / ppe_cfg["model_path"]
    digest = "(파일 없음)"
    if model_path.is_file():
        digest = hashlib.sha256(model_path.read_bytes()).hexdigest()

    alarms = [e for e in events if e["confirmed"]]
    seen_people = [e for e in events if e["people"]]

    lines: list[str] = []
    add = lines.append
    add(f"# PPE 판정 실기 관찰 — {time.strftime('%Y-%m-%d %H:%M')}")
    add("")
    add("FR-9 판정기(`3.7.3`) 구현 전, 학습된 모델이 실기 프레임에서 어떻게 판정하는지")
    add("눈으로 확인한 기록이다. **로봇에는 명령을 보내지 않았다** — 읽기 전용 관찰이다.")
    add("")
    add("## 조건")
    add("")
    add(
        f"- 영상: {'XIAO ' + str(args.xiao_ip) if args.xiao_ip else '이미지 폴더 ' + str(args.images)}"
    )
    add(f"- 프로바이더: {config['vision']['providers']}")
    add(
        f"- 모델: `{ppe_cfg['model_path']}` · 입력 {ppe_cfg['input_size']} · conf {ppe_cfg['conf_threshold']}"
    )
    add(f"- 모델 sha256: `{digest}`")
    add(
        f"- 위반 확정: {window_ms}ms 안 {hits}회"
        + (
            "  (설정값 덮어씀 — 이 PC 가 느려 1.5초 안에 3회가 불가능하다)"
            if args.window_ms or args.hits
            else ""
        )
    )
    add(
        f"- 머리 클리핑 판정: `head_margin_px` {ppe_cfg['head_margin_px']}px"
        + (" · **끔**" if args.no_clip_rule else "")
    )
    add("")
    add("## 결과")
    add("")
    add(f"- 프레임 {frames}장 · {elapsed:.1f}초 · {frames / max(elapsed, 1e-9):.2f} fps")
    add(f"- 사람이 잡힌 프레임 {len(seen_people)}장 / 배경만 {counts.get('사람없음', 0)}장")
    for state in ("적합", "위반", "확인불가"):
        add(f"- {state} {counts.get(state, 0)}건")
    if reasons:
        add("")
        add("확인불가 사유")
        add("")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            add(f"- {reason} {n}건")
    if segment_log is not None:
        segment_section(add, segment_log.snapshot())
    add("")
    add("## 알람 이력")
    add("")
    if alarms:
        add("위반이 시간 창 조건을 채워 확정된 시점이다. 실기라면 여기서 L3 로 올라가고")
        add("경고 방송·눈 LED 적색·스냅샷 기록이 일어난다.")
        add("")
        for e in alarms:
            add(
                f"- {e['t']:.1f}초 · 프레임 `{e['tag']}` · 창 안 {e['hits']}회 · 검출 {e['labels']}"
            )
    else:
        add("확정된 위반이 없다. 아래 둘을 구분해서 봐야 한다.")
        add("")
        add("- 위반 자체가 없었다 — 정상")
        add("- 위반은 보였는데 창을 못 채웠다 — 프레임률이 낮으면 일어난다")
    add("")
    add("## 프레임별 판정")
    add("")
    if seen_people:
        add("사람이 잡힌 프레임만 적는다. 배경만 있는 프레임은 판정 대상이 아니다.")
        add("")
        for e in seen_people:
            mark = " ★확정" if e["confirmed"] else ""
            why = (" / " + ", ".join(e["reasons"])) if e["reasons"] else ""
            add(
                f"- {e['t']:.1f}초 · {e['people']}명 · {', '.join(e['states'])}{why}"
                f" · 검출 {e['labels']}{mark}"
            )
    else:
        add("사람이 잡힌 프레임이 없다.")
    add("")
    add("## 남은 것")
    add("")
    add("- 이 기록은 **동작 확인**이지 성능 판정이 아니다. FR-9 통과 기준은 촬영 조건표대로")
    add("  모은 XIAO 실사 검증셋으로 재며, 그때 오검출률을 함께 잰다")
    add(f"- 추론 속도는 이 PC 기준이다 ({frames / max(elapsed, 1e-9):.2f} fps). 성능 수치는")
    add("  기준 PC(RTX 3080)에서 다시 잰다")
    if args.save_dir:
        add(f"- 판정을 그린 프레임: `{args.save_dir}`")
    add("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XIAO 스트림 PPE 판정 관찰 (읽기 전용)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--xiao-ip", help="카메라 IP. 스트림을 직접 연다")
    source.add_argument("--images", help="폴더 안의 이미지를 대신 쓴다 (카메라 없이 점검)")
    source.add_argument(
        "--webcam",
        type=int,
        metavar="N",
        help="PC 웹캠 N 번을 쓴다. XIAO 가 없을 때 모델 거동만 먼저 보는 용도",
    )
    parser.add_argument("--device", default="mechdog-01", help="개체 프로파일 이름")
    parser.add_argument("--seconds", type=float, default=30.0, help="스트림 관찰 시간")
    parser.add_argument("--crop-pad", type=float, default=0.08, help="person bbox 여유 비율")
    parser.add_argument("--no-clip-rule", action="store_true", help="머리 클리핑 조건을 끈다")
    parser.add_argument("--save-dir", help="판정을 그린 프레임을 저장할 폴더")
    # ⚠️ **설정이 정본이다.** 이 둘은 느린 시험 PC 에서 창을 억지로 맞추기 위한
    # 관찰용 덮어쓰기이며, 실기 판정 값은 `config.vision.ppe` 를 따른다.
    # 추론이 1.5초/프레임인 기계에서 «1.5초 안 3히트» 는 물리적으로 불가능하다.
    parser.add_argument("--window-ms", type=int, help="위반 확정 시간 창 덮어쓰기 (관찰용)")
    parser.add_argument("--hits", type=int, help="위반 확정 히트 수 덮어쓰기 (관찰용)")
    parser.add_argument("--show", action="store_true", help="창으로 띄운다 (GUI 필요)")
    parser.add_argument(
        "--web-port", type=int, default=8088, help="판정 화면을 내보낼 포트. 0 이면 끈다"
    )
    parser.add_argument("--report", help="시험 결과를 정리한 MD 를 쓸 경로")
    parser.add_argument(
        "--segments",
        action="store_true",
        help="웹 화면에 촬영 구간 버튼을 띄운다. 누른 구간이 정답이 되어 오판정을 센다",
    )
    parser.add_argument(
        "--wait-min",
        type=float,
        default=0.0,
        help="카메라가 살아날 때까지 이만큼(분) 기다린 뒤 시작한다",
    )
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="0.0.0.0 으로 두면 같은 공유기의 다른 기기에서도 본다",
    )
    args = parser.parse_args(argv)

    import cv2

    # ⚠️ 배경 실행에서 표준출력이 버퍼에 갇혀 «아무 일도 안 하는 것» 처럼 보였다.
    sys.stdout.reconfigure(line_buffering=True)

    config = load_config(args.device)
    if args.xiao_ip:
        config["network"]["xiao_ip"] = args.xiao_ip

    ppe_cfg = config["vision"]["ppe"]
    head_margin = int(ppe_cfg["head_margin_px"])
    use_clip = bool(ppe_cfg["require_head_visible"]) and not args.no_clip_rule

    coco = Detector(config, section="coco", labels=COCO_CLASSES)
    ppe = Detector(config, section="ppe", labels=list(ppe_cfg["classes"]))
    print("모델 적재 중 — 첫 프레임 비용을 여기서 지불한다")
    coco.open()
    ppe.open()

    window_ms = int(args.window_ms or ppe_cfg.get("violation_window_ms", 1500))
    hits = int(args.hits or ppe_cfg.get("violation_hits_required", 3))
    window = ViolationWindow(window_ms, hits)
    print(f"프로바이더 {config['vision']['providers']}")
    print(
        f"위반 확정 조건 {window_ms}ms 안 {hits}회"
        + ("  ← 설정값 덮어씀 (관찰용)" if args.window_ms or args.hits else "")
    )
    relay = FrameRelay()
    segment_log = SegmentLog() if args.segments else None
    if args.web_port:
        start_web(relay, args.web_host, args.web_port, segment_log)
    elif segment_log is not None:
        print("⚠️ --segments 는 웹 화면에서 누르는 버튼이다. --web-port 를 끄면 쓸 수 없다")

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    counts: collections.Counter = collections.Counter()
    reasons: collections.Counter = collections.Counter()
    #: 알람 이력. **확정만 적지 않는다** — 매 프레임 판정을 남겨야 «왜 안 떴나» 를
    #: 나중에 추적할 수 있다. 확정은 그중 표시만 다르게 둔다.
    events: list[dict[str, Any]] = []
    frames = 0
    started = time.monotonic()

    def handle(image: np.ndarray, tag: str) -> None:
        nonlocal frames
        frames += 1
        results = process(
            image,
            coco,
            ppe,
            person_label=config["vision"]["coco"]["person_class"],
            pad=args.crop_pad,
            head_margin=head_margin,
            use_clip=use_clip,
        )
        if not results:
            counts["사람없음"] += 1
        violating = any(j.state == STATE_VIOLATION for _, j, _ in results)
        newly = window.observe(violating, int(time.monotonic() * 1000))

        for person, judged, origin in results:
            counts[judged.state] += 1
            if judged.reason:
                reasons[judged.reason] += 1
            annotate(image, person, judged, origin)

        if segment_log is not None:
            segment_log.observe([j.state for _, j, _ in results])

        elapsed_s = round(time.monotonic() - started, 1)
        events.append(
            {
                "t": elapsed_s,
                "tag": tag,
                "people": len(results),
                "states": [j.state for _, j, _ in results],
                "reasons": [j.reason for _, j, _ in results if j.reason],
                "labels": sorted({d.label for _, j, _ in results for d in j.detections}),
                "confirmed": bool(newly),
                "hits": window.hits,
            }
        )
        if newly:
            print(f"  [{tag}] ★ 위반 확정 — 창 안 {window.hits}회 (실기라면 여기서 L3)")
        elif results:
            summary = " · ".join(
                f"{j.state}{'/' + j.reason if j.reason else ''}" for _, j, _ in results
            )
            print(f"  [{tag}] 사람 {len(results)}명 — {summary}")

        if args.web_port:
            ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                relay.publish(buf.tobytes())
        if save_dir:
            cv2.imwrite(str(save_dir / f"{tag}.jpg"), image)
        if args.show:
            cv2.imshow("ppe_live_check", image)
            cv2.waitKey(1)

    if args.webcam is not None:
        # ⚠️ **XIAO 와 같은 화면이 아니다.** 센서도 ISP 도 JPEG 압축도 다르므로 여기서 잰
        # 숫자를 FR-9 통과 판정에 쓰지 않는다(OI-13). 카메라가 없거나 전원이 불안정할 때
        # 모델 거동과 판정 규칙을 먼저 보는 용도다.
        capture = cv2.VideoCapture(args.webcam, cv2.CAP_DSHOW if sys.platform == "win32" else 0)
        if not capture.isOpened():
            print(f"웹캠 {args.webcam} 을 열지 못했다 — 번호를 바꿔 본다")
            return 2
        print(f"웹캠 {args.webcam} — {args.seconds:.0f}초 관찰")
        try:
            index = 0
            while time.monotonic() - started < args.seconds:
                ok, image = capture.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                index += 1
                handle(image, f"cam{index:05d}")
        except KeyboardInterrupt:
            print("\n중단됨")
        finally:
            capture.release()
    elif args.images:
        folder = Path(args.images)
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        if not files:
            print(f"이미지가 없다: {folder}")
            return 2
        print(f"이미지 {len(files)}장 — 카메라 없이 점검")
        for path in files:
            buf = np.fromfile(str(path), dtype=np.uint8)
            image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if image is None:
                continue
            handle(image, path.stem)
    else:
        if args.wait_min > 0 and not wait_for_camera(config, args.wait_min):
            return 2
        reader = StreamReader(config)
        print(f"스트림 {reader.url} — {args.seconds:.0f}초 관찰")
        print("⚠️ 스트림은 한 클라이언트만 받는다. 브라우저 탭이 열려 있으면 한 장도 못 받는다.")

        # ⚠️ **프레임이 한 장도 안 오면 아래 for 루프의 시간 검사는 영영 돌지 않는다.**
        # 실제로 카메라 전원이 빠진 상태에서 재접속만 무한히 반복했다. 관찰 시간은
        # «프레임을 받는 동안» 이 아니라 **벽시계 기준**이어야 한다.
        def stop_after_deadline() -> None:
            deadline = started + args.seconds + 5.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
            reader.stop()

        threading.Thread(target=stop_after_deadline, daemon=True).start()
        try:
            for frame in reader.frames():
                image = decode_jpeg(frame.payload)
                if image is not None:
                    handle(image, f"{frame.seq:05d}")
                if time.monotonic() - started >= args.seconds:
                    break
        except KeyboardInterrupt:
            print("\n중단됨")
        finally:
            reader.stop()

    elapsed = time.monotonic() - started
    print(f"\n프레임 {frames}장 · {elapsed:.1f}초 ({frames / max(elapsed, 1e-9):.1f} fps)")
    print(f"판정 {dict(counts)}")
    if reasons:
        print(f"확인불가 사유 {dict(reasons)}")
    if args.report:
        write_report(
            Path(args.report),
            args,
            config,
            window_ms,
            hits,
            frames,
            elapsed,
            counts,
            reasons,
            events,
            segment_log,
        )
        print(f"보고서 {args.report}")
    if args.show:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
