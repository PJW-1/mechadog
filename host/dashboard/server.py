"""PC 로컬 FastAPI/WS 서버 — 텔레메트리 방송과 명령 API (WBS 4.5.1 · 4.5.3).

⚠️ **명령 API 가 붙으면서 더 이상 읽기 전용이 아니다.** `commands` 를 넘기지
않으면 예전처럼 읽기 전용으로 뜨고, 넘기면 `/api/command/*` 가 열린다. 이
경로는 **로봇을 실제로 움직이므로** WebSocket 과 같은 로컬 출처 검사를 건다.

영상 송출은 후속 WBS 다.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from anyio import CancelScope
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from host.dashboard.commands import CommandService
from host.dashboard.state import DashboardState

PERIOD_S = 0.1
SEND_TIMEOUT_S = 1.0
MAX_CLIENTS = 16


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


async def _send_updates(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        message = await queue.get()
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await websocket.send_json(message)


async def _receive_close(websocket: WebSocket) -> int | None:
    message = await websocket.receive()
    # close는 송신 태스크를 회수한 다음 핸들러 하나에서만 보낸다.
    return None if message["type"] == "websocket.disconnect" else 1008


def _local_origins(port: int) -> set[str]:
    """이 서버 자신을 가리키는 출처만 허용한다.

    명령 경로가 붙었으므로 이 검사는 **로봇이 움직이는 것을 막는 문**이다.
    브라우저의 다른 탭에서 들어온 요청이 순찰을 멈추게 두지 않는다.
    """
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if port == 80:
        allowed.update({"http://127.0.0.1", "http://localhost"})
    return allowed


def create_app(state: DashboardState, commands: CommandService | None = None) -> FastAPI:
    hub = TelemetryHub(state)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(hub.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="MechDog telemetry", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/health")
    async def health():
        return {
            "service": "telemetry",
            "read_only": commands is None,
            "clients": len(hub.clients),
            "coalesced_updates": hub.coalesced,
        }

    @app.get("/api/telemetry")
    async def telemetry():
        return state.snapshot()

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
                asyncio.create_task(_send_updates(websocket, queue)),
                asyncio.create_task(_receive_close(websocket)),
            ]
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for pending in _pending:
                pending.cancel()
            await asyncio.gather(*_pending, return_exceptions=True)
            for task in done:
                if task.result() == 1008:
                    async with asyncio.timeout(SEND_TIMEOUT_S):
                        await websocket.close(code=1008, reason="Telemetry channel is read-only")
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

    return app


@contextmanager
def running_server(state: DashboardState, port: int) -> Iterator[uvicorn.Server]:
    """기존 동기 운용 루프와 별도 스레드에서 실행한다. 로컬 인터페이스만 사용한다."""
    ready = threading.Event()
    failures: list[BaseException] = []

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = LocalServer(
        uvicorn.Config(
            create_app(state),
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
