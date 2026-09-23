"""PC 로컬 FastAPI/WS 서버 — 텔레메트리·검출 방송과 명령 API (WBS 4.5.1 · 4.5.2 · 4.5.3).

⚠️ **명령 API 가 붙으면서 더 이상 읽기 전용이 아니다.** `commands` 를 넘기지
않으면 예전처럼 읽기 전용으로 뜨고, 넘기면 `/api/command/*` 가 열린다. 이
경로는 **로봇을 실제로 움직이므로** WebSocket 과 같은 로컬 출처 검사를 건다.

카메라 영상은 XIAO 스트림이 **단일 클라이언트**라 비전 워커가 점유한 채널을
뺏으면 추론이 끊긴다. 그래서 여기서는 XIAO 에 새로 붙지 않고 워커가 방금
추론에 쓴 JPEG 를 재송출한다 — 화면에 보이는 것이 곧 판정에 들어간 것이다.

검출 오버레이(`/ws/vision` · WBS 4.5.2)는 **박스를 계산한 바로 그 JPEG 와 박스를
한 메시지로** 보낸다. 영상 스트림과 박스를 따로 보내면 추론 지연만큼 박스가
다른 장면 위에 그려진다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from anyio import CancelScope
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from host.behavior.mission import available_modes
from host.dashboard.commands import CommandService
from host.dashboard.state import EVENT_BUFFER, DashboardState

PERIOD_S = 0.1
SEND_TIMEOUT_S = 1.0
MAX_CLIENTS = 16
#: 사건 방송 주기. 사람 감지는 초당 여러 번 나오는 일이 아니므로 촘촘할 필요가
#: 없고, 운용 스레드의 기록과 브라우저 사이를 잇기만 하면 된다.
EVENT_POLL_S = 0.2
CAMERA_PERIOD_S = 0.1
# 새 추론 결과가 나왔는지 보는 주기. ⚠️ **추론 주기(25fps = 40ms)보다 짧아야 한다.**
# 0.1 이던 때는 확인 사이에 나온 결과가 버려져 화면이 초당 10장으로 묶였다 —
# 4.5.2 실측 "초당 9.8" 이 추론률이 아니라 이 상한이었다. 한 번 보는 일은 최신
# 참조를 꺼내 같은 객체인지 비교하는 것뿐이다. 기존 MJPEG 폴링 주기와 섞지 않는다.
VISION_POLL_PERIOD_S = 0.01
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"
# 관제 화면은 빌드 없이 이 폴더를 그대로 내보낸다. three.js 도 `static/vendor/` 에
# 함께 싣는다 — 설치에 기대면 빠뜨렸을 때 app.js 가 첫 import 에서 실패해 화면의
# 조작이 통째로 죽는다(3D 만 비는 것이 아니다). 검사는 `npm run check`.


class TelemetryHub:
    """10Hz 상태 방송. 느린 연결마다 최신 한 건만 남기며 다른 연결을 기다리지 않는다."""

    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.clients: set[asyncio.Queue] = set()
        self.coalesced = 0

    def subscribe(self) -> asyncio.Queue | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.clients.add(queue)
        return queue

    def broadcast(self) -> None:
        message = self.state.snapshot()
        for queue in self.clients:
            if queue.full():
                queue.get_nowait()
                self.coalesced += 1
            queue.put_nowait(message)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while True:
            self.broadcast()
            deadline += PERIOD_S
            # 지연된 방송을 몰아서 보내지 않는다. 센서 측정/AI 프레임은 건드리지 않는다.
            now = loop.time()
            if deadline <= now:
                deadline += (int((now - deadline) / PERIOD_S) + 1) * PERIOD_S
            await asyncio.sleep(max(0, deadline - loop.time()))


def encode_vision_frame(result: Any) -> bytes:
    """검출 결과 하나를 WS 바이너리 메시지 하나로 만든다.

    ``[헤더 길이 uint32 big-endian][UTF-8 JSON 헤더][JPEG]``

    ⚠️ **텍스트 헤더와 JPEG 를 두 메시지로 나누지 않는다.** 송신 시간 초과로 둘째가
    취소되면 다음 JPEG 가 앞 헤더와 짝지어져 박스가 엉뚱한 사진에 그려진다.
    박스는 원본 픽셀 좌표 ``[x1, y1, x2, y2]`` 이고 ``width``·``height`` 로 화면에
    맞춰 늘린다. 필드 이름은 블랙박스 기록과 같다.
    """
    header = {
        "type": "vision",
        "frame_seq": result.frame_seq,
        "width": result.frame_width,
        "height": result.frame_height,
        "completed_ms": result.completed_ms,
        "detections": [
            {"label": d.label, "score": round(d.score, 3), "box": [round(v, 1) for v in d.box]}
            for d in result.detections
        ],
        "tracks": [
            {
                "track_id": t.track_id,
                "score": round(t.score, 3),
                "box": [round(v, 1) for v in t.box],
            }
            for t in result.tracks
        ],
    }
    raw = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw + result.jpeg


class EventHub:
    """사건 방송 (WBS 4.4.3 · FR-3.9). **텔레메트리와 달리 합치지 않는다.**

    ⚠️ **최신 한 건만 남기면 안 된다.** 텔레메트리는 상태라서 늦은 연결에 옛 값을
    버려도 손해가 없지만, 사건은 *"그때 사람이 있었다"* 는 기록이다. 합치면 그
    기록이 사라지고 화면에는 아무 일도 없던 것처럼 보인다.

    ⚠️ **늦게 붙어도 최근 사건을 본다.** 브라우저는 로봇보다 늦게 열리고 새로고침도
    한다. 붙는 순간 버퍼에 있는 것을 먼저 넘기고, 버퍼에서 밀려 못 주는 것이 있으면
    `event_gap` 으로 **말해 준다** — 조용히 넘기면 사람이 공백을 정상으로 읽는다.
    """

    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.clients: set[asyncio.Queue] = set()
        self.cursor = state.event_seq
        self.overflowed = 0

    def subscribe(self) -> asyncio.Queue | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_BUFFER + 1)
        backlog, dropped = self.state.events_since(0)
        if dropped:
            queue.put_nowait({"type": "event_gap", "dropped": dropped})
        for event in backlog[-EVENT_BUFFER:]:
            queue.put_nowait(event)
        self.clients.add(queue)
        return queue

    def broadcast(self) -> None:
        events, _dropped = self.state.events_since(self.cursor)
        if not events:
            return
        self.cursor = events[-1]["seq"]
        for queue in self.clients:
            for event in events:
                if queue.full():
                    # 이 연결은 사건을 따라오지 못한다. 오래된 것을 버리고 **버린
                    # 사실을 남긴다** — 조용히 사라지는 것이 가장 나쁘다.
                    queue.get_nowait()
                    self.overflowed += 1
                queue.put_nowait(event)

    async def run(self) -> None:
        """사건은 드물다 — 상태를 짧게 들여다보고 새것만 흘린다."""
        while True:
            self.broadcast()
            await asyncio.sleep(EVENT_POLL_S)


class VisionHub:
    """검출 프레임 방송. **새 추론 결과가 나왔을 때만**, 연결마다 최신 한 장만 남긴다."""

    def __init__(self, source: Callable[[], Any]) -> None:
        self.source = source
        self.clients: set[asyncio.Queue] = set()
        self.coalesced = 0
        self._last: Any = None
        self._last_message: bytes | None = None

    def subscribe(self) -> asyncio.Queue | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        # 새 화면은 다음 추론을 기다리지 않고 마지막 프레임부터 받는다 — 카메라가
        # 멈춰 있으면 영영 빈 화면이 된다. 낡았는지는 `completed_ms` 로 가린다.
        if self._last_message is not None:
            queue.put_nowait(self._last_message)
        self.clients.add(queue)
        return queue

    def broadcast(self) -> bool:
        result = self.source()
        # 워커는 추론마다 결과 객체를 새로 만든다. 같은 객체면 같은 추론이다 — seq 는
        # 카메라가 다시 붙으면 처음부터 다시 셀 수 있어 비교 기준으로 쓰지 않는다.
        if result is None or result is self._last or not result.jpeg:
            return False
        self._last = result
        message = self._last_message = encode_vision_frame(result)
        for queue in self.clients:
            if queue.full():
                queue.get_nowait()
                self.coalesced += 1
            queue.put_nowait(message)
        return True

    async def run(self) -> None:
        while True:
            self.broadcast()
            await asyncio.sleep(VISION_POLL_PERIOD_S)


async def _send_updates(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        message = await queue.get()
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await websocket.send_json(message)


async def _send_frames(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        message = await queue.get()
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await websocket.send_bytes(message)


async def _receive_close(websocket: WebSocket) -> int | None:
    message = await websocket.receive()
    # close는 송신 태스크를 회수한 다음 핸들러 하나에서만 보낸다.
    return None if message["type"] == "websocket.disconnect" else 1008


async def _serve_subscriber(
    websocket: WebSocket,
    hub: TelemetryHub | VisionHub,
    send: Callable[[WebSocket, asyncio.Queue], Awaitable[None]],
    read_only_reason: str,
) -> None:
    """방송 채널 하나의 연결 수명 — 출처 검사, 연결 상한, 느린 연결 차단, 회수."""
    origin = websocket.headers.get("origin")
    allowed_origins = _local_origins(websocket.url.port or 80)
    if origin is not None and origin not in allowed_origins:
        await websocket.close(code=1008)
        return
    queue = hub.subscribe()
    if queue is None:
        await websocket.close(code=1013)
        return
    tasks: list[asyncio.Task] = []
    try:
        await websocket.accept()
        tasks = [
            asyncio.create_task(send(websocket, queue)),
            asyncio.create_task(_receive_close(websocket)),
        ]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for pending in _pending:
            pending.cancel()
        await asyncio.gather(*_pending, return_exceptions=True)
        for task in done:
            if task.result() == 1008:
                async with asyncio.timeout(SEND_TIMEOUT_S):
                    await websocket.close(code=1008, reason=read_only_reason)
    except TimeoutError:
        with contextlib.suppress(TimeoutError, OSError, RuntimeError):
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await websocket.close(code=1013, reason="Client too slow")
    except (WebSocketDisconnect, OSError, asyncio.CancelledError):
        pass
    finally:
        hub.clients.discard(queue)
        for task in tasks:
            task.cancel()
        # ASGI 접속 수명이 취소돼도 연결별 송수신 태스크는 회수한다.
        with CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)


def _local_origins(port: int) -> set[str]:
    """이 서버 자신을 가리키는 출처만 허용한다.

    명령 경로가 붙었으므로 이 검사는 **로봇이 움직이는 것을 막는 문**이다.
    브라우저의 다른 탭에서 들어온 요청이 순찰을 멈추게 두지 않는다.
    """
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if port == 80:
        allowed.update({"http://127.0.0.1", "http://localhost"})
    return allowed


class _RevalidatedStatic(StaticFiles):
    """정적 파일마다 `Cache-Control: no-cache` 를 붙인다.

    ⚠️ **없으면 브라우저가 옛 화면을 계속 보여 준다.** `StaticFiles` 는 ETag 와
    `Last-Modified` 만 주고 `Cache-Control` 을 주지 않는데, 그러면 브라우저가
    스스로 신선도를 추정해서 재검증 없이 사본을 쓴다. 2026-09-22 실기에서
    **새로 넣은 «경보 확인 (L3 해제)» 버튼이 화면에 나오지 않았다** — 서버는 새
    파일을 내려주고 있었고 브라우저가 옛 사본을 쥐고 있었다.

    ⚠️ **버튼이 없는 것보다 이 쪽이 위험하다.** 같은 화면이 `FSM IDLE`·
    `래치 해제됨` 을 보여 주는 동안 실제 상태는 `TRACK`·**L3** 였다. 없는 버튼은
    눈에 보이지만 틀린 단계는 눈에 보이지 않는다.

    `no-cache` 는 *"저장하지 말라"* 가 아니라 *"쓰기 전에 물어보라"* 다. ETag 가
    그대로면 304 만 오가므로 대역은 거의 늘지 않는다.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    state: DashboardState,
    commands: CommandService | None = None,
    camera: Callable[[], bytes | None] | None = None,
    static_dir: Path | None = None,
    vision: Callable[[], Any] | None = None,
    event_snapshot: Callable[[str], bytes | None] | None = None,
    policy: dict[str, Any] | None = None,
) -> FastAPI:
    hub = TelemetryHub(state)
    vision_hub = VisionHub(vision) if vision is not None else None
    event_hub = EventHub(state)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [asyncio.create_task(hub.run()), asyncio.create_task(event_hub.run())]
        if vision_hub is not None:
            tasks.append(asyncio.create_task(vision_hub.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="MechDog telemetry", lifespan=lifespan)
    app.state.hub = hub
    app.state.event_hub = event_hub

    @app.get("/health")
    async def health():
        return {
            "service": "telemetry",
            # 이 서버가 어느 개체 프로파일로 떴는지 — 여러 런타임을 띄웠을 때 가려내는 데 쓴다.
            "device_id": state.snapshot()["device_id"],
            "read_only": commands is None,
            "clients": len(hub.clients),
            "coalesced_updates": hub.coalesced,
            # 비전이 없으면 null — "연결 0" 과 "채널 없음" 을 구분한다.
            "vision_clients": None if vision_hub is None else len(vision_hub.clients),
            "event_clients": len(event_hub.clients),
            # 사건을 따라오지 못해 버린 건수. 0 이 아니면 화면이 기록을 놓쳤다.
            "events_overflowed": event_hub.overflowed,
            # 지금 이 저장소에서 **고를 수 있는** 운용 모드 (FR-11.7). 선행 기능이
            # 없는 모드는 빠진다.
            # ⚠️ **화면은 아직 이 값을 읽지 않는다 (2026-09-19 · 의도된 선택).** 모드
            # 버튼 셋을 늘 띄워 두고 **누르면 서버가 사유를 돌려준다** — FR-11.7 이
            # 요구하는 것은 «거부한다» 이지 «버튼을 숨겨라» 가 아니고, 못 고르는 이유가
            # 화면에 남는 편이 «버튼이 왜 없지» 보다 낫다. 여기 실어 두는 것은 운용자가
            # 서버에 직접 물어볼 수 있게 하기 위해서다.
            # ⚠️ 상태 전문에 싣지 않는다. 10Hz 로 흐르는 값이 아니라 기동 시점에
            # 정해지는 사실이고, 매 주기 실으면 대역만 먹는다.
            "modes": list(available_modes()),
        }

    @app.get("/api/telemetry")
    async def telemetry():
        return state.snapshot()

    @app.get("/api/policy")
    async def policy_values():
        """설정 화면이 보여 줄 대응 단계·인증 값 (B7). 기동 시 읽은 config 그대로다.

        없으면 404 — 화면은 값을 지어내지 않고 «설정값 미수신» 으로 적는다.
        """
        if policy is None:
            return JSONResponse({"error": "no_policy"}, status_code=404)
        return policy

    @app.get("/api/events")
    async def events(since: int = 0):
        """`since` 순번 뒤의 사건을 돌려준다 — WS 를 못 쓰는 쪽(음성 저널)을 위한 폴링 경로.

        `/ws/events` 와 같은 버퍼다. `dropped` 가 0 이 아니면 버퍼에서 밀려
        못 주는 사건이 있었다는 뜻이니 조용히 넘기지 않는다 (4.4.3 규약).
        """
        found, dropped = state.events_since(since)
        return {
            "events": found,
            "dropped": dropped,
            "latest": state.event_seq,
        }

    @app.get("/events/{entry}/snapshot.jpg")
    async def event_snapshot_image(entry: str):
        """사건 하나의 저장된 그림. **지금 화면이 아니라 그때 장면이다.**

        ⚠️ **사건 전문에 JPEG 를 싣지 않기 때문에 이 경로가 필요하다**(`4.4.3`) —
        프레임 하나가 수십 KB 라 사건 소켓에 실으면 텔레메트리를 밀어낸다. 대신
        디렉터리 이름만 보내고 그림은 여기서 꺼낸다.

        ⚠️ **이름 검증은 블랙박스가 한다.** 저장 구조를 아는 것이 거기뿐이고,
        여기서 경로를 조립하면 규칙이 두 곳에 생겨 한쪽만 고쳐질 수 있다.
        """
        jpeg = event_snapshot(entry) if event_snapshot is not None else None
        if jpeg is None:
            return JSONResponse({"error": "no_snapshot"}, status_code=404)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            # 사건 그림은 바뀌지 않는다 — 한 번 받으면 다시 받지 않게 둔다.
            headers={"Cache-Control": "private, max-age=3600"},
        )

    if camera is not None:

        @app.get("/camera/snapshot.jpg")
        async def camera_snapshot():
            jpeg = camera()
            if jpeg is None:
                return JSONResponse({"error": "no_frame"}, status_code=503)
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/camera/stream")
        async def camera_stream():
            async def frames() -> Iterator[bytes]:
                last: bytes | None = None
                while True:
                    jpeg = camera()
                    if jpeg is not None and jpeg is not last:
                        last = jpeg
                        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    await asyncio.sleep(CAMERA_PERIOD_S)

            return StreamingResponse(
                frames(), media_type="multipart/x-mixed-replace; boundary=frame"
            )

    if commands is not None:

        def _rejected_origin(request: Request) -> JSONResponse | None:
            origin = request.headers.get("origin")
            if origin is None:
                return None  # 브라우저가 아닌 도구(curl·시험)는 출처를 붙이지 않는다
            if origin in _local_origins(request.url.port or 80):
                return None
            return JSONResponse({"error": "origin"}, status_code=403)

        @app.post("/api/command/estop")
        async def estop(request: Request):
            """**어떤 상태에서도 통한다.** 조건을 검사하지 않는다 (FR-4.4)."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            return commands.estop().as_dict()

        @app.post("/api/command/manual")
        async def manual(request: Request):
            """`{"on": true|false}` 로 수동 오버라이드를 잡거나 놓는다."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            on = body.get("on")
            if not isinstance(on, bool):
                return JSONResponse({"error": "on"}, status_code=400)
            result = commands.manual_on() if on else commands.manual_off()
            return result.as_dict()

        @app.post("/api/command/patrol")
        async def patrol(request: Request):
            """`{"action": "start"|"stop"}` — 시작은 예약, 정지는 수동 경유로 `IDLE` 에 정착."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            action = body.get("action")
            if action == "start":
                return commands.patrol().as_dict()
            if action == "stop":
                return commands.patrol_stop().as_dict()
            return JSONResponse({"error": "action"}, status_code=400)

        @app.post("/api/command/reset")
        async def reset(request: Request):
            """사람이 원인 해소를 확인한 뒤 누르는 `FAILSAFE` 해제 요청."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            return commands.reset().as_dict()

        @app.post("/api/command/alarm")
        async def alarm(request: Request):
            """사람이 상황을 확인한 뒤 누르는 **경보(L3) 해제** (FR-10.3.2).

            ⚠️ **`/api/command/reset` 과 다른 문이다.** 저쪽은 물리 상태(F)를
            확인하고 로봇의 래치 보고를 기다리며, 이쪽은 상황 판단이라 로봇에
            보낼 것이 없다. 하나로 묶으면 **비상정지를 눌렀다 푸는 것으로 경보가
            지워진다** ([ADR-26]).

            ⚠️ **이 문이 없으면 헤드리스 런타임은 경보를 풀 수 없다** — 콘솔
            확인 키는 tty 를 요구한다(`runtime.watch_console`).
            """
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            return commands.alarm_confirm().as_dict()

        @app.post("/api/command/service")
        async def service(request: Request):
            """`{"mode": "enter"|"exit"}` 로 온보드 서비스 모드를 전환한다.

            진입은 로봇을 주차시키고 루프 워치독을 건다(패치·진단용). 해제 후에도
            safe 래치는 남으므로 보행 복귀에는 `/api/command/reset` 이 필요하다.
            """
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            mode = body.get("mode")
            if not isinstance(mode, str):
                return JSONResponse({"error": "mode"}, status_code=400)
            return commands.service(mode).as_dict()

        @app.post("/api/command/mode")
        async def mission_mode(request: Request):
            """`{"mode": "guard"|"factory"}` — 운용 모드 전환 (FR-4.7 · FR-11.3).

            ⚠️ **온보드 `SERVICE` 와 다른 축이다.** 저쪽은 OTA·진단 중 액추에이터를
            차단하는 정비 상태이고, 이쪽은 정상 운용 중 Tier 2 판단을 고르는 임무
            모드다. 경로를 나눠 둔 이유가 그것이다.
            """
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            mode = body.get("mode")
            if not isinstance(mode, str):
                return JSONResponse({"error": "mode"}, status_code=400)
            return commands.mission_mode(mode).as_dict()

        @app.post("/api/command/auth")
        async def auth(request: Request):
            """`{"result": "ok"|"fail"|"pending", "captured_at_ms"?: int}` — 음성 암구호 경로.

            대조 자체는 음성 파이프라인이 한다 — 여기는 판정을 FSM 사건으로
            옮기는 자리일 뿐이다. `AUTH_WAIT` 가 아니면 거절된다 (WBS 3.8.2).

            `captured_at_ms` 는 **사람이 말한 시각**(epoch ms)이며 선택이다.
            싣고 오면 런타임이 `AUTH_WAIT` 가 열린 시각과 견주어 **창이 열리기
            전에 녹음된 발화를 시도로 세지 않는다.** 녹음·전사에 수 초가 걸려
            «말한 시각» 과 «판정이 도착한 시각» 이 다르기 때문이다.

            `"pending"` 은 **판정이 아니다** — 발화를 받아 두었고 전사가 도는
            중이라는 통지이며, `auth.timeout_s` 마감을 `verdict_grace_s` 만큼
            **창마다 한 번** 미룬다 (ADR-37). 상한이 없으면 소리만 계속 내서
            경보를 영영 막을 수 있다.
            """
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            result = body.get("result")
            if result not in ("ok", "fail", "pending"):
                return JSONResponse({"error": "result"}, status_code=400)
            captured_at_ms = body.get("captured_at_ms")
            # ⚠️ **`bool` 을 정수로 받지 않는다.** `isinstance(True, int)` 가 참이라
            # `captured_at_ms: true` 가 시각 1 로 들어가 **모든 발화가 오래된 것**이
            # 되어 인증이 통째로 막힌다.
            if captured_at_ms is not None and (
                isinstance(captured_at_ms, bool) or not isinstance(captured_at_ms, int)
            ):
                return JSONResponse({"error": "captured_at_ms"}, status_code=400)
            return commands.auth(result, captured_at_ms).as_dict()

        @app.post("/api/command/drive")
        async def drive(request: Request):
            """`MANUAL` 에서만 받는다. 다음 틱에 반영된다."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            try:
                step = float(body["step"])
                angle = float(body["angle"])
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"error": "fields"}, status_code=400)
            return commands.drive(step, angle).as_dict()

        @app.post("/api/command/pose")
        async def pose(request: Request):
            """`{"preset": "up"|"level"|"down"}` — `MANUAL` 에서만 받는다. 다음 틱에 반영된다."""
            rejected = _rejected_origin(request)
            if rejected is not None:
                return rejected
            body = await request.json()
            preset = body.get("preset")
            if not isinstance(preset, str):
                return JSONResponse({"error": "preset"}, status_code=400)
            return commands.pose(preset).as_dict()

    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket):
        await _serve_subscriber(websocket, hub, _send_updates, "Telemetry channel is read-only")

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        await _serve_subscriber(websocket, event_hub, _send_updates, "Event channel is read-only")

    if vision_hub is not None:

        @app.websocket("/ws/vision")
        async def websocket_vision(websocket: WebSocket):
            await _serve_subscriber(
                websocket, vision_hub, _send_frames, "Vision channel is read-only"
            )

    if static_dir is not None and static_dir.is_dir():
        # API·WS 경로를 먼저 등록해 두고 마지막에 붙인다 — mount 는 등록 순서대로
        # 탐색하므로 `/api/*`·`/camera/*`·`/ws/*` 는 위의 처리기가 받는다.
        app.mount("/", _RevalidatedStatic(directory=static_dir, html=True), name="web")

    return app


#: 플릿 화면에서 로봇 하나의 API 가 붙는 경로. `/robots/<id>/api/...` · `/robots/<id>/ws/...`
FLEET_PREFIX = "/robots"


def create_fleet_app(
    robots: dict[str, FastAPI],
    *,
    registered: dict[str, bool] | None = None,
    static_dir: Path | None = DEFAULT_STATIC_DIR,
) -> FastAPI:
    """여러 대를 한 서버·한 출처로 (`host.fleet`).

    로봇마다 `create_app` 로 만든 앱을 `/robots/<id>` 에 붙인다. 명령·WS·출처 검사는
    한 대짜리와 **같은 코드**다 — 여러 대를 위해 명령 경로를 새로 쓰지 않는다.

    ⚠️ **붙인 앱의 lifespan 은 Starlette 가 돌려 주지 않는다.** 방송 루프가 거기서
    시작하므로, 그대로 두면 WS 가 연결은 되고 아무것도 오지 않는다. 여기서 직접 연다.
    """
    marks = registered or {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            for sub in robots.values():
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    app = FastAPI(title="MechDog fleet", lifespan=lifespan)

    @app.get("/health")
    async def health():
        # `service` 가 같아야 화면이 이 출처를 API 로 인정한다 (`app.js` resolveApiBase).
        return {"service": "telemetry", "fleet": list(robots), "modes": list(available_modes())}

    @app.get("/api/fleet")
    async def fleet():
        return {
            "robots": [
                {
                    "id": robot_id,
                    "base": f"{FLEET_PREFIX}/{robot_id}",
                    "registered": marks.get(robot_id, True),
                }
                for robot_id in robots
            ]
        }

    for robot_id, sub in robots.items():
        app.mount(f"{FLEET_PREFIX}/{robot_id}", sub, name=f"robot-{robot_id}")

    @app.websocket("/{path:path}")
    async def unknown_ws(websocket: WebSocket):
        # ⚠️ `/` 의 정적 마운트는 http 범위만 받는다 — 매칭되지 않은 WS(예: 예전
        # 한 대짜리 화면의 /ws/telemetry 재연결)가 거기로 새면 assertion 으로 터진다.
        # 정적 마운트보다 **먼저** 등록해야 라우터가 WS 를 여기서 잡는다.
        await websocket.close(code=1008)

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", _RevalidatedStatic(directory=static_dir, html=True), name="web")
    return app


@contextmanager
def running_server(
    state: DashboardState,
    port: int,
    commands: CommandService | None = None,
    camera: Callable[[], bytes | None] | None = None,
    static_dir: Path | None = DEFAULT_STATIC_DIR,
    vision: Callable[[], Any] | None = None,
    event_snapshot: Callable[[str], bytes | None] | None = None,
    policy: dict[str, Any] | None = None,
) -> Iterator[uvicorn.Server]:
    """기존 동기 운용 루프와 별도 스레드에서 실행한다. 로컬 인터페이스만 사용한다."""
    app = create_app(
        state,
        commands=commands,
        camera=camera,
        static_dir=static_dir,
        vision=vision,
        event_snapshot=event_snapshot,
        policy=policy,
    )
    with serving(app, port) as server:
        yield server


@contextmanager
def serving(app: FastAPI, port: int) -> Iterator[uvicorn.Server]:
    """앱 하나를 별도 스레드에서 띄운다. 로컬 인터페이스만 사용한다."""
    ready = threading.Event()
    failures: list[BaseException] = []

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = LocalServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            ws_max_size=1024,
            ws_max_queue=1,
            timeout_graceful_shutdown=2,
        )
    )
    # bind 실패는 운용 시작 전에 호출자에게 전달한다. 포트 충돌을 숨기지 않는다.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(32)

        def run() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:
                failures.append(exc)
            finally:
                ready.set()

        thread = threading.Thread(target=run, name="dashboard", daemon=True)
        thread.start()
        try:
            # 첫 기동은 import·모델 준비로 5초를 넘긴 적이 있다 (2026-09-23 확인 서버).
            if not ready.wait(15) or not server.started:
                raise RuntimeError("Dashboard startup failed") from (
                    failures[0] if failures else None
                )
            yield server
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=1)
            if thread.is_alive():
                raise RuntimeError("Dashboard shutdown timed out")
