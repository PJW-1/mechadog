"""VLM 적재 수명주기 검증 (WBS 4.8.0 · ADR-35 결정 5 (2026-09-24 개정: 상시 적재)).

**기동할 때 한 번 올리고 종료할 때만 내린다.** 모드마다 올리고 내리던 때는 판독 시작과
해제가 겹치는 경쟁이 셋 있었다 — 운행 중에 내리지 않으면 셋 다 사라진다. 여기서 지키는
것은 넷이다.

    ① 어느 모드로 기동해도 기동하며 올린다 — 생성만으로는 올리지 않는다
    ② 모드를 바꿔도 올리거나 내리지 않는다
    ③ 종료(`release`)는 돌고 있는 판독이 끝난 뒤에 내린다
    ④ 적재 도중에 종료해도 적재를 끝까지 기다리지 않는다

⚠️ **적재를 `threading.Event` 로 붙잡는다.** 실제 14.5초를 기다리지 않고 «올리는 중»
이라는 창을 원하는 만큼 벌려 둔다. 기다림은 전부 제한 시간이 있다.
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import FakeClock
from test_runtime import DEVICE, FakeSocket, FakeVision

from host.behavior.mission import Mission
from host.runtime import Runtime
from host.vision.vlm_reader import VlmReader
from host.vision.vlm_worker import JOIN_TIMEOUT_S

WAIT_S = 5.0


class GateSession:
    """판독을 `ask_gate` 가 열릴 때까지 붙잡는 세션. 닫힐 때 판독 중이었는지 적는다."""

    def __init__(self, ask_gate: threading.Event | None = None) -> None:
        self._ask_gate = ask_gate
        self._in_ask = False
        self.asking = threading.Event()
        self.closed = threading.Event()
        self.closed_while_asking = False

    def ask(self, _image: object, _prompt: str) -> str:
        self._in_ask = True
        self.asking.set()
        if self._ask_gate is not None:
            self._ask_gate.wait(WAIT_S)
        self._in_ask = False
        return "yes"

    def close(self) -> None:
        self.closed_while_asking = self._in_ask
        self.closed.set()


class GateFactory:
    """적재를 `gate` 가 열릴 때까지 붙잡는 세션 팩토리 — 14.5초 적재의 대역."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        #: 두 번째 적재가 시작됐다 — **이것이 서면 한 번 내렸다가 다시 올린 것이다**
        self.entered_again = threading.Event()
        self.calls = 0
        self.sessions: list[GateSession] = []

    def __call__(self) -> GateSession:
        self.calls += 1
        (self.entered_again if self.entered.is_set() else self.entered).set()
        self.gate.wait(WAIT_S)
        session = GateSession()
        self.sessions.append(session)
        return session


def _runtime(cfg: dict, clock: FakeClock, reader: VlmReader, *, mode: str) -> Runtime:
    return Runtime(
        cfg,
        device_id=DEVICE,
        clock=clock,
        vision=FakeVision(),
        mission=Mission(cfg, mode=mode),
        vlm_reader=reader,
    )


def _join_load_threads() -> None:
    """적재 스레드가 끝나기를 기다린다. 고정 sleep 이 아니라 제한 시간 join."""
    for thread in threading.enumerate():
        if thread.name == "vlm-load":
            thread.join(WAIT_S)


# ── ① 어느 모드로 기동해도 올린다 ───────────────────────────


@pytest.mark.usefixtures("unlock_modes")
@pytest.mark.parametrize("mode", ["guard", "factory"])
def test_boot_starts_loading_in_any_mode(cfg: dict, clock: FakeClock, mode: str) -> None:
    """경비 모드에 올라가 있어도 판독은 생기지 않는다 — 구역 점검이 공장 모드에서만 돈다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode=mode)
    assert factory.calls == 0, "생성만으로 적재가 돌면 시험마다 모델이 올라간다"

    runtime.begin(FakeSocket(clock))

    assert factory.entered.wait(WAIT_S), f"{mode} 모드로 기동했는데 적재를 시작하지 않았다"
    # 올리는 동안은 판독을 받지 않는다 — 호출부는 `not_loaded` 로 남기고 지나간다.
    assert runtime.vlm.submit(b"jpeg", now_ms=1) is False
    factory.gate.set()
    _join_load_threads()
    assert runtime.vlm.available is True


# ── ② 모드를 바꿔도 올리거나 내리지 않는다 ─────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_mode_switches_neither_load_nor_unload(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 운행 중에 내리면 판독 시작과 해제가 겹친다 — 세션을 닫거나 VRAM 이 남던 곳이다."""
    factory = GateFactory()
    factory.gate.set()
    reader = VlmReader(factory)
    reader.load()
    runtime = _runtime(cfg, clock, reader, mode="factory")

    assert runtime.set_mode("guard") is None
    # 해제가 돌 틈을 준다. 고치기 전 코드는 이 사이에 내린다.
    closed = factory.sessions[0].closed.wait(0.3)
    assert runtime.set_mode("factory") is None
    factory.entered_again.wait(0.3)

    assert closed is False, "경비로 바꿨는데 내렸다"
    assert factory.calls == 1, f"팩토리가 {factory.calls}번 불렸다 — 모드를 바꿀 때 다시 올렸다"
    assert runtime.vlm.available is True


# ── ③ 종료가 판독이 끝난 뒤에 내린다 ───────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_release_unloads_after_the_running_read(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 추론 중인 세션을 닫으면 판독 스레드가 닫힌 모델을 만진다."""
    ask_gate = threading.Event()
    session = GateSession(ask_gate)
    reader = VlmReader(lambda: session, budget_ms=10_000)
    reader.load()
    runtime = _runtime(cfg, clock, reader, mode="factory")
    assert runtime.vlm.submit(b"jpeg", now_ms=1) is True
    assert session.asking.wait(WAIT_S)

    releaser = threading.Thread(target=runtime.release)
    releaser.start()
    # 종료가 닫을 틈을 준다. 판독을 기다리지 않는 코드는 이 사이에 판독 중인 세션을 닫는다.
    session.closed.wait(0.5)
    ask_gate.set()
    releaser.join(WAIT_S)

    assert session.closed.is_set(), "종료했는데 VRAM 을 놓지 않았다"
    assert session.closed_while_asking is False, "판독 중인 세션을 닫았다"
    assert runtime.vlm.available is False


@pytest.mark.usefixtures("unlock_modes")
def test_release_unloads_the_reader(cfg: dict, clock: FakeClock) -> None:
    session = GateSession()
    reader = VlmReader(lambda: session, budget_ms=10_000)
    reader.load()
    runtime = _runtime(cfg, clock, reader, mode="factory")

    runtime.release()

    assert session.closed.is_set(), "종료했는데 VLM 을 내리지 않았다"
    assert runtime.vlm.available is False


# ── ④ 적재 도중의 종료 ─────────────────────────────────────


def test_release_does_not_wait_out_a_load(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 적재(14.5초) 도중에 끝내도 종료가 상한을 넘겨 막히지 않는다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode="guard")
    runtime.begin(FakeSocket(clock))
    assert factory.entered.wait(WAIT_S)

    started = time.monotonic()
    runtime.release()
    elapsed = time.monotonic() - started

    assert elapsed < JOIN_TIMEOUT_S + 1.0, f"종료가 {elapsed:.1f}초 막혔다"
    factory.gate.set()
    _join_load_threads()
