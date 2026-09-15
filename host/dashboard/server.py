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


def create_app(
    state: DashboardState,
    commands: CommandService | None = None,
    camera: Callable[[], bytes | None] | None = None,
    static_dir: Path | None = None,
    vision: Callable[[], Any] | None = None,
    event_snapshot: Callable[[str], bytes | None] | None = None,
    overview_url: str | None = None,
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
            # 화면의 연결 전환 목록이 런타임마다 어느 개체를 보는지 표시하는 데 쓴다.
            "device_id": state.snapshot()["device_id"],
            # 시뮬 조망 스트림 — 대시보드가 시뮬 공장을 메인 화면으로 띄울 때 쓴다.
            # 실기 개체는 키가 없어 null 이다.
            "overview_url": overview_url,
            "read_only": commands is None,
            "clients": len(hub.clients),
            "coalesced_updates": hub.coalesced,
            # 비전이 없으면 null — "연결 0" 과 "채널 없음" 을 구분한다.
            "vision_clients": None if vision_hub is None else len(vision_hub.clients),
            "event_clients": len(event_hub.clients),
            # 사건을 따라오지 못해 버린 건수. 0 이 아니면 화면이 기록을 놓쳤다.
            "events_overflowed": event_hub.overflowed,
        }

    @app.get("/api/telemetry")
    async def telemetry():
        return state.snapshot()

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

    live_page = Path(__file__).resolve().parent / "static" / "live.html"
    if live_page.is_file():
        # 최소 실기 화면 — 디자인 프로토타입과 무관하게 카메라+이동만 단독 동작한다.
        live_html = live_page.read_text(encoding="utf-8")

        @app.get("/live")
        async def live():
            return Response(content=live_html, media_type="text/html")

    if static_dir is not None and static_dir.is_dir():
        # API·WS 경로를 먼저 등록해 두고 마지막에 붙인다 — mount 는 등록 순서대로
        # 탐색하므로 `/api/*`·`/camera/*`·`/ws/*` 는 위의 처리기가 받는다.
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")

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
    overview_url: str | None = None,
) -> Iterator[uvicorn.Server]:
    """기존 동기 운용 루프와 별도 스레드에서 실행한다. 로컬 인터페이스만 사용한다."""
    ready = threading.Event()
    failures: list[BaseException] = []

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = LocalServer(
        uvicorn.Config(
            create_app(
                state,
                commands=commands,
                camera=camera,
                static_dir=static_dir,
                vision=vision,
                event_snapshot=event_snapshot,
                overview_url=overview_url,
            ),
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
            if not ready.wait(5) or not server.started:
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
