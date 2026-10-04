"""PC 로컬 FastAPI/WS 서버 — 텔레메트리·검출·사건 방송과 명령 API.

⚠️ `commands` 를 넘기면 `/api/command/*` 가 열리고, 그 경로는 로봇을 실제로 움직이므로
WebSocket 과 같은 로컬 출처 검사(`_require_local_origin`)를 건다. 넘기지 않으면 읽기 전용이다.

카메라 영상은 XIAO 에 새로 붙지 않고(단일 클라이언트) 워커가 추론에 쓴 JPEG 를 재송출한다.
`/ws/vision` 은 그 JPEG 와 박스를 한 메시지로 보낸다 (ADR-32).

서버는 운용 루프와 다른 스레드(자체 이벤트 루프)에서 돈다. 명령은 `commands` 를 거친다 — E-Stop·
수동 전환은 즉시 적용되고, 해제·확인·조종 입력은 운용 루프의 다음 틱에 반영된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import uvicorn
from anyio import CancelScope
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)
from starlette.concurrency import run_in_threadpool

from host.behavior.mission import available_modes
from host.cloud.broadcast import Broadcaster
from host.dashboard.commands import CommandService
from host.dashboard.planning import PlanError, PlanInput, PlanningService
from host.dashboard.state import EVENT_BUFFER, DashboardState

if TYPE_CHECKING:
    from host.vision.worker import VisionResult

PERIOD_S = 0.1
SEND_TIMEOUT_S = 1.0
MAX_CLIENTS = 16
#: 사건 방송 주기.
EVENT_POLL_S = 0.2
CAMERA_PERIOD_S = 0.1
# 새 추론 결과가 나왔는지 보는 주기 — 추론 주기(25fps = 40ms)보다 짧아야 화면이 추론률을 따라간다.
VISION_POLL_PERIOD_S = 0.01
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 흰색 관제 화면(#314). 본체 index도 같은 테마를 직접 읽는다. 정적 폴더와 같은
#: 부모 아래에 있으면 `/glass-preview/` 로 내보내고 `/` 를 그리로 보낸다. **검정(과거 버전)
#: 화면은 구버전이다** — 2026-10-02 운용자 결정. `/index.html` 은 흰색 화면이 iframe 으로 쓰므로 남긴다.
GLASS_DIR_NAME = "glass-preview"
GLASS_PATH = "/glass-preview"
# 관제 화면은 빌드 없이 이 폴더를 그대로 내보낸다. three.js 도 `static/vendor/` 에 싣는다
# (검사는 `npm run check`).


class TelemetryHub:
    """10Hz 상태 방송. 느린 연결마다 최신 한 건만 남기며 다른 연결을 기다리지 않는다."""

    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.clients: set[asyncio.Queue[Any]] = set()
        self.coalesced = 0

    def subscribe(self) -> asyncio.Queue[Any] | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
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


def encode_vision_frame(result: VisionResult) -> bytes:
    """검출 결과 하나를 WS 바이너리 메시지 하나로 만든다.

    ``[헤더 길이 uint32 big-endian][UTF-8 JSON 헤더][JPEG]``

    헤더와 JPEG 를 한 메시지로 묶어 박스가 다른 사진과 짝지어지지 않게 한다 (ADR-32).
    박스는 원본 픽셀 좌표 ``[x1, y1, x2, y2]`` 이고 필드 이름은 블랙박스 기록과 같다.
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
    """사건 방송 (FR-3.9). 텔레메트리와 달리 최신 한 건으로 합치지 않는다 — 사건은 기록이다.

    붙는 순간 버퍼의 최근 사건을 먼저 넘기고, 버퍼에서 밀려 못 준 것은 `event_gap` 으로 알린다.
    """

    def __init__(self, state: DashboardState) -> None:
        self.state = state
        self.clients: set[asyncio.Queue[Any]] = set()
        self.cursor = state.event_seq
        self.overflowed = 0

    def subscribe(self) -> asyncio.Queue[Any] | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=EVENT_BUFFER + 1)
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
                    # 이 연결은 사건을 따라오지 못한다 — 오래된 것을 버리고 버린 사실을 남긴다.
                    queue.get_nowait()
                    self.overflowed += 1
                queue.put_nowait(event)

    async def run(self) -> None:
        """상태를 짧게 들여다보고 새 사건만 흘린다."""
        while True:
            self.broadcast()
            await asyncio.sleep(EVENT_POLL_S)


class VisionHub:
    """검출 프레임 방송. 새 추론 결과가 나왔을 때만, 연결마다 최신 한 장만 남긴다."""

    def __init__(self, source: Callable[[], Any]) -> None:
        self.source = source
        self.clients: set[asyncio.Queue[Any]] = set()
        self.coalesced = 0
        self._last: Any = None
        self._last_message: bytes | None = None

    def subscribe(self) -> asyncio.Queue[Any] | None:
        if len(self.clients) >= MAX_CLIENTS:
            return None
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
        # 새 화면은 마지막 프레임부터 받는다. 낡았는지는 `completed_ms` 로 가린다.
        if self._last_message is not None:
            queue.put_nowait(self._last_message)
        self.clients.add(queue)
        return queue

    def broadcast(self) -> bool:
        result = self.source()
        # 워커는 추론마다 결과 객체를 새로 만든다 — 같은 객체면 같은 추론이다(seq 는 재연결 때 다시 센다).
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


async def _send_updates(websocket: WebSocket, queue: asyncio.Queue[Any]) -> None:
    while True:
        message = await queue.get()
        async with asyncio.timeout(SEND_TIMEOUT_S):
            await websocket.send_json(message)


async def _send_frames(websocket: WebSocket, queue: asyncio.Queue[Any]) -> None:
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
    hub: TelemetryHub | VisionHub | EventHub,
    send: Callable[[WebSocket, asyncio.Queue[Any]], Coroutine[Any, Any, None]],
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
    tasks: list[asyncio.Task[Any]] = []
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

    ⚠️ 명령 경로의 출입문이다 — 다른 출처의 브라우저 탭이 로봇을 움직이지 못하게 한다.
    """
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if port == 80:
        allowed.update({"http://127.0.0.1", "http://localhost"})
    return allowed


class _RevalidatedStatic(StaticFiles):
    """정적 파일마다 `Cache-Control: no-cache` 를 붙인다 — 브라우저가 옛 화면 사본을 재검증 없이
    쓰지 않게(관제 화면이 실제 상태와 다른 것을 보여 주지 않게). ETag 가 같으면 304 만 오간다."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class _RefusedError(Exception):
    """요청 거절 — `create_app` 이 `{"error": <사유>}` 와 상태 코드로 응답한다."""

    def __init__(self, error: str, status_code: int) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code


async def _refused_response(_request: Request, exc: _RefusedError) -> JSONResponse:
    return JSONResponse({"error": exc.error}, status_code=exc.status_code)


async def _require_local_origin(request: Request) -> None:
    """상태를 바꾸는 경로의 출처 검사. 다른 출처면 403 `{"error": "origin"}`."""
    origin = request.headers.get("origin")
    # 브라우저가 아닌 도구(curl·시험)는 출처를 붙이지 않는다
    if origin is not None and origin not in _local_origins(request.url.port or 80):
        raise _RefusedError("origin", 403)


_LOCAL_ORIGIN = [Depends(_require_local_origin)]


def _as_float(value: Any) -> float:
    """`float()` 그대로 바꾼다(문자열 숫자·bool 도 받는다). 못 바꾸면 검증 오류다."""
    try:
        return float(value)
    except (TypeError, OverflowError) as exc:
        raise ValueError(str(exc)) from exc


class _ManualBody(BaseModel):
    on: StrictBool


class _PatrolBody(BaseModel):
    action: Literal["start", "stop"]


class _ZoneBody(BaseModel):
    zone: StrictStr


class _ModeBody(BaseModel):
    mode: StrictStr


class _SoundBody(BaseModel):
    # `bool` 을 정수로 받지 않는다 — `true` 가 트랙 1 이 된다.
    track: StrictInt


class _AuthBody(BaseModel):
    result: Literal["ok", "fail", "pending"]
    # `bool` 을 정수로 받지 않는다 — `true` 가 시각 1 이 되면 모든 발화가 오래된 것이 된다.
    captured_at_ms: StrictInt | None = None


class _DriveBody(BaseModel):
    step: Annotated[float, BeforeValidator(_as_float)]
    angle: Annotated[float, BeforeValidator(_as_float)]


class _GotoBody(BaseModel):
    x: Annotated[float, BeforeValidator(_as_float)]
    y: Annotated[float, BeforeValidator(_as_float)]


class _PoseBody(BaseModel):
    preset: StrictStr


class _BroadcastPatch(BaseModel):
    # 기본값 None 은 «필드 없음» 표시일 뿐이다 — 명시한 null 은 거절한다. 있는지는 `model_fields_set`.
    volume: Annotated[StrictInt, Field(ge=0, le=100)] = None  # type: ignore[assignment]
    muted: StrictBool = None  # type: ignore[assignment]


async def _read_body[B: BaseModel](
    request: Request, model: type[B], invalid: str | None = None
) -> B:
    """본문 JSON 객체를 `model` 로 읽는다. 틀리면 400 으로 거절한다.

    사유는 처음 틀린 필드 이름이고, 본문이 JSON 객체가 아니면(깨진 JSON 포함) `"body"` 다.
    `invalid` 를 주면 어떤 잘못이든 그 사유 하나로 낸다.
    """
    try:
        body = await request.json()
    except ValueError:  # 깨진 JSON·잘못된 인코딩
        body = None
    if not isinstance(body, dict):
        raise _RefusedError(invalid or "body", 400)
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        detail = str(first["loc"][0]) if first["loc"] else first["msg"]
        raise _RefusedError(invalid or detail, 400) from exc


def _broadcast_routes(broadcast: Broadcaster | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/broadcast", response_model=None)
    async def broadcast_status() -> dict[str, Any]:
        """PC 스피커 방송(관제 TTS)의 음량·무음 상태. 로봇 스피커(`/api/command/sound`)와는 별개다.

        방송기가 없으면(piper 미설치 등) `available: false` — 화면은 «방송 없음» 을 보여 준다.
        """
        if broadcast is None:
            return {"available": False}
        return {"available": True, "volume": broadcast.volume, "muted": broadcast.muted}

    @router.post("/api/broadcast", dependencies=_LOCAL_ORIGIN, response_model=None)
    async def broadcast_update(request: Request) -> dict[str, Any] | JSONResponse:
        """`{"volume"?: 0..100, "muted"?: bool}` — 온 필드만 바꾼다.

        음량은 범위 밖이면 거절한다(자르지 않는다). fleet 은 방송기가 하나라 모든 로봇 화면에
        같은 상태가 보인다.
        """
        if broadcast is None:
            return JSONResponse({"error": "unavailable"}, status_code=404)
        # 두 필드를 모두 검증한 뒤 적용한다 — 음량만 바꾸고 400 을 내면 화면과 실제가 어긋난다.
        patch = await _read_body(request, _BroadcastPatch)
        if "volume" in patch.model_fields_set:
            broadcast.set_volume(patch.volume)
        if "muted" in patch.model_fields_set:
            broadcast.set_muted(patch.muted)
        return {"available": True, "volume": broadcast.volume, "muted": broadcast.muted}

    return router


def _command_routes(commands: CommandService) -> APIRouter:
    """`/api/command/*` — 로봇을 실제로 움직이므로 모든 경로에 출처 검사를 건다."""
    router = APIRouter(prefix="/api/command", dependencies=_LOCAL_ORIGIN)

    @router.post("/estop", response_model=None)
    async def estop() -> dict[str, object]:
        """어떤 상태에서도 통한다 — 조건을 검사하지 않는다 (FR-4.4)."""
        return commands.estop().as_dict()

    @router.post("/manual", response_model=None)
    async def manual(request: Request) -> dict[str, object]:
        """`{"on": true|false}` 로 수동 오버라이드를 잡거나 놓는다."""
        body = await _read_body(request, _ManualBody)
        result = commands.manual_on() if body.on else commands.manual_off()
        return result.as_dict()

    @router.post("/patrol", response_model=None)
    async def patrol(request: Request) -> dict[str, object]:
        """`{"action": "start"|"stop"}` — 시작은 예약, 정지는 수동 경유로 `IDLE` 에 정착."""
        body = await _read_body(request, _PatrolBody)
        if body.action == "start":
            return commands.patrol().as_dict()
        return commands.patrol_stop().as_dict()

    @router.post("/reset", response_model=None)
    async def reset() -> dict[str, object]:
        """사람이 원인 해소를 확인한 뒤 누르는 `FAILSAFE` 해제 요청."""
        return commands.reset().as_dict()

    @router.post("/alarm", response_model=None)
    async def alarm() -> dict[str, object]:
        """사람이 상황을 확인한 뒤 누르는 경보(L3) 해제 (FR-10.3.2).

        `/api/command/reset`(F 해제)과 다른 문이다 (ADR-26 ①). 헤드리스 런타임의 유일한
        L3 해제 경로이기도 하다(콘솔 키는 tty 를 요구한다).
        """
        return commands.alarm_confirm().as_dict()

    @router.post("/zone-baseline", response_model=None)
    async def zone_baseline(request: Request) -> dict[str, object]:
        """`{"zone": "A"}` — 관리자가 인정한 구역의 기준을 지워 그 구역을 다음에 볼 때(점검 중이면 이번 장면) 새로 뜨게 한다.

        물건을 영구히 옮긴 경우의 문이다 (ADR-41 결정 6). 런타임이 다음 틱에 지운다.
        `zones.ids` 에 없는 구역은 `accepted=false` 다. 경보(L3)는 풀지 않는다.
        """
        body = await _read_body(request, _ZoneBody)
        return commands.zone_baseline(body.zone).as_dict()

    @router.post("/goto", response_model=None)
    async def goto(request: Request) -> dict[str, object] | JSONResponse:
        """`{"x": 1.2, "y": -0.4}` (순찰 좌표 m) — 지도에서 찍은 곳으로 간다.

        예약만 한다. 경로 유무·자기 위치 확인은 다음 틱에 판정되어 `/api/nav` 에 실린다.
        """
        body = await _read_body(request, _GotoBody)
        if not (math.isfinite(body.x) and math.isfinite(body.y)):
            return JSONResponse({"error": "x"}, status_code=400)
        return commands.goto(body.x, body.y).as_dict()

    @router.post("/locate", response_model=None)
    async def locate(request: Request) -> dict[str, object]:
        """`{"zone": "C"}` — 사람이 로봇이 지금 있는 구역을 알려준다. 그 구역 안에서만 위치를 다시 찾는다.

        들어 옮긴 뒤처럼 전역 탐색이 집 안 비슷한 자리를 구별 못 할 때 쓴다. 지금 자세의
        신뢰는 버려지고 로봇은 다시 찾을 때까지 선다. 없는 구역은 `accepted=false` 다.
        """
        body = await _read_body(request, _ZoneBody)
        return commands.locate(body.zone).as_dict()

    @router.post("/service", response_model=None)
    async def service(request: Request) -> dict[str, object]:
        """`{"mode": "enter"|"exit"}` 로 온보드 서비스 모드를 전환한다.

        진입은 로봇을 주차시키고 루프 워치독을 건다(패치·진단용). 해제 후에도
        safe 래치는 남으므로 보행 복귀에는 `/api/command/reset` 이 필요하다.
        """
        body = await _read_body(request, _ModeBody)
        return commands.service(body.mode).as_dict()

    @router.post("/sound", response_model=None)
    async def sound(request: Request) -> dict[str, object]:
        """`{"track": 0..3000}` — 로봇 MP3 모듈 트랙 재생, 0 = 정지.

        음성 프로세스(`robotlink.play_track`)가 자기 발화를 로봇 스피커로 트는
        문이다. FAILSAFE 래치 중에도 받는다(펌웨어와 같다). 범위 밖은
        `accepted=false` 로 돌려주고 로봇에 보내지 않는다. `accepted` 는
        «다음 틱에 싣는다» 이지 «소리가 났다» 가 아니다.
        """
        body = await _read_body(request, _SoundBody)
        return commands.sound(body.track).as_dict()

    @router.post("/mode", response_model=None)
    async def mission_mode(request: Request) -> dict[str, object]:
        """`{"mode": "guard"|"factory"}` — 운용 모드 전환 (FR-4.7 · FR-11.3).

        온보드 `SERVICE`(정비 상태)와 다른 축이다 — 이쪽은 Tier 2 판단을 고르는 임무 모드다.
        """
        body = await _read_body(request, _ModeBody)
        return commands.mission_mode(body.mode).as_dict()

    @router.post("/auth", response_model=None)
    async def auth(request: Request) -> dict[str, object]:
        """`{"result": "ok"|"fail"|"pending", "captured_at_ms"?: int}` — 음성 암구호 경로.

        대조 자체는 음성 파이프라인이 한다 — 여기는 판정을 FSM 사건으로
        옮기는 자리일 뿐이다. `AUTH_WAIT` 가 아니면 거절된다.

        `captured_at_ms` 는 사람이 말한 시각(epoch ms, 선택)이다 — 창이 열리기 전 발화는
        시도로 세지 않는다. `"pending"` 은 판정이 아니라 전사 중 통지이며 `auth.timeout_s`
        마감을 `verdict_grace_s` 만큼 한 번 미룬다 (ADR-37).
        """
        body = await _read_body(request, _AuthBody)
        return commands.auth(body.result, body.captured_at_ms).as_dict()

    @router.post("/drive", response_model=None)
    async def drive(request: Request) -> dict[str, object]:
        """`MANUAL` 에서만 받는다. 다음 틱에 반영된다."""
        body = await _read_body(request, _DriveBody, invalid="fields")
        return commands.drive(body.step, body.angle).as_dict()

    @router.post("/pose", response_model=None)
    async def pose(request: Request) -> dict[str, object]:
        """`{"preset": "up"|"level"|"down"}` — `MANUAL` 에서만 받는다. 다음 틱에 반영된다."""
        body = await _read_body(request, _PoseBody)
        return commands.pose(body.preset).as_dict()

    return router


def _mount_dashboard(app: FastAPI, static_dir: Path | None) -> None:
    """단일·플릿 서버가 같은 흰색 기본 화면을 제공한다."""
    if static_dir is None or not static_dir.is_dir():
        return
    glass_dir = static_dir.parent / GLASS_DIR_NAME
    if glass_dir.is_dir():

        @app.get("/", include_in_schema=False)
        async def white_dashboard(request: Request) -> RedirectResponse:
            query = "?" + request.url.query if request.url.query else ""
            return RedirectResponse(f"{GLASS_PATH}/{query}", status_code=307)

        app.mount(GLASS_PATH, _RevalidatedStatic(directory=glass_dir, html=True), name="glass")
    app.mount("/static", _RevalidatedStatic(directory=static_dir, html=True), name="static")
    app.mount("/", _RevalidatedStatic(directory=static_dir, html=True), name="web")


def _planning_routes(planning: PlanningService) -> APIRouter:
    router = APIRouter(prefix="/api/planning", dependencies=_LOCAL_ORIGIN)

    async def call[T](method: Callable[..., T], *args: Any) -> T:
        try:
            return await run_in_threadpool(method, *args)
        except PlanError as exc:
            raise _RefusedError(str(exc), exc.status) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise _RefusedError(
                "운용 설정을 읽거나 저장하지 못했습니다. 서버 설정 파일을 확인하세요.", 503
            ) from exc

    @router.get("")
    async def snapshot() -> dict[str, Any]:
        return await call(planning.snapshot)

    @router.get("/map.png")
    async def map_image() -> Response:
        return Response(
            await call(planning.map_png),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/preview")
    async def preview(request: Request) -> dict[str, Any]:
        plan = await _read_body(request, PlanInput)
        return await call(planning.preview, plan)

    @router.post("")
    async def save(request: Request) -> dict[str, Any]:
        plan = await _read_body(request, PlanInput)
        return await call(planning.save, plan)

    return router


def create_app(
    state: DashboardState,
    commands: CommandService | None = None,
    camera: Callable[[], bytes | None] | None = None,
    static_dir: Path | None = None,
    vision: Callable[[], Any] | None = None,
    event_snapshot: Callable[[str], bytes | None] | None = None,
    policy: dict[str, Any] | None = None,
    broadcast: Broadcaster | None = None,
    map_view: Callable[[], tuple[bytes, dict[str, Any]]] | None = None,
    nav_status: Callable[[], dict[str, Any]] | None = None,
    planning: PlanningService | None = None,
) -> FastAPI:
    hub = TelemetryHub(state)
    vision_hub = VisionHub(vision) if vision is not None else None
    event_hub = EventHub(state)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
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
    app.add_exception_handler(
        _RefusedError,
        cast("Callable[[Request, Exception], Awaitable[Response]]", _refused_response),
    )

    @app.get("/health", response_model=None)
    async def health() -> dict[str, Any]:
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
            # 지금 이 저장소에서 고를 수 있는 운용 모드 (FR-11.7). 화면은 이 값으로 버튼을
            # 숨기지 않고, 못 고르는 모드는 누르면 서버가 사유를 돌려준다. 기동 때 정해지는
            # 값이라 10Hz 상태 전문에는 싣지 않는다.
            "modes": list(available_modes()),
        }

    @app.get("/api/telemetry", response_model=None)
    async def telemetry() -> dict[str, Any]:
        return state.snapshot()

    @app.get("/api/map/meta", response_model=None)
    async def map_meta() -> dict[str, Any] | JSONResponse:
        """실제 집 지도의 크기·좌표 변환 행렬·구역. LiDAR 측위 순찰이 아니면 404."""
        if map_view is None:
            return JSONResponse({"error": "no_map"}, status_code=404)
        return (await asyncio.to_thread(map_view))[1]

    @app.get("/api/map.png", response_model=None)
    async def map_png() -> Response:
        if map_view is None:
            return JSONResponse({"error": "no_map"}, status_code=404)
        png, _meta = await asyncio.to_thread(map_view)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/nav", response_model=None)
    async def nav() -> dict[str, Any] | JSONResponse:
        """로봇의 자기 위치·신뢰·목표·구역 (순찰 좌표). 화면이 주기적으로 묻는다."""
        if nav_status is None:
            return JSONResponse({"error": "no_nav"}, status_code=404)
        return nav_status()

    @app.get("/api/policy", response_model=None)
    async def policy_values() -> dict[str, Any] | JSONResponse:
        """설정 화면이 보여 줄 대응 단계·인증 값 (B7). 기동 시 읽은 config 그대로다.

        없으면 404 — 화면은 값을 지어내지 않고 «설정값 미수신» 으로 적는다.
        """
        if policy is None:
            return JSONResponse({"error": "no_policy"}, status_code=404)
        return policy

    @app.get("/api/events", response_model=None)
    async def events(since: int = 0) -> dict[str, Any]:
        """`since` 순번 뒤의 사건을 돌려준다 — WS 를 못 쓰는 쪽(음성 저널)을 위한 폴링 경로.

        `/ws/events` 와 같은 버퍼다. `dropped` 가 0 이 아니면 버퍼에서 밀려 못 준 사건이 있었다.
        """
        found, dropped = state.events_since(since)
        return {
            "events": found,
            "dropped": dropped,
            "latest": state.event_seq,
        }

    @app.get("/events/{entry}/snapshot.jpg")
    async def event_snapshot_image(entry: str) -> Response:
        """사건 하나의 저장된 그림(그때 장면). 사건 전문은 JPEG 대신 디렉터리 이름만 싣는다.

        ⚠️ 이름은 신뢰할 수 없는 입력이며 검증은 `EventBlackbox.snapshot_bytes` 한 곳이 한다.
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
        async def camera_snapshot() -> Response:
            jpeg = camera()
            if jpeg is None:
                return JSONResponse({"error": "no_frame"}, status_code=503)
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/camera/stream")
        async def camera_stream() -> StreamingResponse:
            async def frames() -> AsyncIterator[bytes]:
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

    app.include_router(_broadcast_routes(broadcast))
    if planning is not None:
        app.include_router(_planning_routes(planning))
    if commands is not None:
        app.include_router(_command_routes(commands))

    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket) -> None:
        await _serve_subscriber(websocket, hub, _send_updates, "Telemetry channel is read-only")

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket) -> None:
        await _serve_subscriber(websocket, event_hub, _send_updates, "Event channel is read-only")

    if vision_hub is not None:

        @app.websocket("/ws/vision")
        async def websocket_vision(websocket: WebSocket) -> None:
            await _serve_subscriber(
                websocket, vision_hub, _send_frames, "Vision channel is read-only"
            )

    _mount_dashboard(app, static_dir)

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

    로봇마다 `create_app` 로 만든 앱을 `/robots/<id>` 에 붙인다(명령·WS·출처 검사는 같은 코드).
    붙인 앱의 lifespan(방송 루프)은 Starlette 가 돌려 주지 않으므로 여기서 직접 연다.
    """
    marks = registered or {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for sub in robots.values():
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    app = FastAPI(title="MechDog fleet", lifespan=lifespan)

    @app.get("/health", response_model=None)
    async def health() -> dict[str, Any]:
        # `service` 가 같아야 화면이 이 출처를 API 로 인정한다 (`app.js` resolveApiBase).
        return {"service": "telemetry", "fleet": list(robots), "modes": list(available_modes())}

    @app.get("/api/fleet", response_model=None)
    async def fleet() -> dict[str, Any]:
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
    async def unknown_ws(websocket: WebSocket) -> None:
        # `/` 의 정적 마운트는 http 범위만 받으므로, 매칭되지 않은 WS 를 잡는 경로를 먼저 등록한다.
        await websocket.close(code=1008)

    _mount_dashboard(app, static_dir)
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
    broadcast: Broadcaster | None = None,
    map_view: Callable[[], tuple[bytes, dict[str, Any]]] | None = None,
    nav_status: Callable[[], dict[str, Any]] | None = None,
    planning: PlanningService | None = None,
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
        broadcast=broadcast,
        map_view=map_view,
        nav_status=nav_status,
        planning=planning,
    )
    with serving(app, port) as server:
        yield server


@contextmanager
def serving(app: FastAPI, port: int) -> Iterator[uvicorn.Server]:
    """앱 하나를 별도 스레드에서 띄운다. 로컬 인터페이스만 사용한다."""
    ready = threading.Event()
    failures: list[BaseException] = []

    class LocalServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
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
            # 첫 기동은 import·모델 준비로 5초를 넘길 수 있다.
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
