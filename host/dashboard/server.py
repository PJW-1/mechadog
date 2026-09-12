"""PC 로컬 읽기 전용 FastAPI/WS 서버. 명령 API와 영상은 후속 WBS다."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

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


def create_app(state: DashboardState) -> FastAPI:
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
            "read_only": True,
            "clients": len(hub.clients),
            "coalesced_updates": hub.coalesced,
        }

    @app.get("/api/telemetry")
    async def telemetry():
        return state.snapshot()

    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        port = websocket.url.port or 80
        allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
        if port == 80:
            allowed_origins.update({"http://127.0.0.1", "http://localhost"})
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
