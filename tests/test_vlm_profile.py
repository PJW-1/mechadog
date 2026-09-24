"""VLM 적재 프로파일 수명주기 검증 (WBS 4.8.0 · ADR-35 결정 5).

**모드가 곧 적재 프로파일이다.** VLM(4.1GB)은 `factory` 에서만 올라가야 하고, 10GB
카드라 두 벌이 올라가면 넘친다. 여기서 지키는 것은 다섯이다.

    ① 적재 중에 모드를 바꿔도 마지막 모드로 수렴한다
    ② factory 를 연타해도 한 번만 올린다
    ③ 돌고 있는 판독이 끝난 뒤에 세션을 닫는다
    ④ 종료(`release`)가 모델을 내린다 — 오래 막히지 않고
    ⑤ 공장 모드로 기동하면 기동하며 올린다 — 생성만으로는 올리지 않는다

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
        #: 두 번째 적재가 시작됐다 — **이것이 서면 4.1GB 가 두 벌이다**
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


def _join_profile_threads() -> None:
    """적재·해제 스레드가 끝나기를 기다린다. 고정 sleep 이 아니라 제한 시간 join."""
    for thread in threading.enumerate():
        if thread.name == "vlm-profile":
            thread.join(WAIT_S)


# ── ① 마지막 모드로 수렴한다 ────────────────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_leaving_factory_during_load_unloads_once_loaded(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 적재 중에는 `loaded` 가 거짓이라 «이미 내려가 있다» 로 읽으면 VRAM 이 남는다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode="guard")

    assert runtime.set_mode("factory") is None
    assert factory.entered.wait(WAIT_S)
    assert runtime.set_mode("guard") is None  # 올리는 도중에 떠난다
    factory.gate.set()

    _join_profile_threads()
    assert factory.sessions[0].closed.wait(WAIT_S), "경비 모드인데 VRAM 에 남았다"
    assert runtime.vlm.available is False


# ── ② 두 벌 올리지 않는다 ───────────────────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_repeated_factory_entry_loads_only_once(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 대시보드 연타. 4.1GB 가 두 벌 올라가면 10GB 를 넘긴다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode="guard")

    for _ in range(3):
        assert runtime.set_mode("factory") is None
    assert factory.entered.wait(WAIT_S)
    # 두 번째 적재가 시작될 틈을 준다. 고치기 전 코드는 이 사이에 팩토리를 또 부른다.
    factory.entered_again.wait(0.3)
    factory.gate.set()
    _join_profile_threads()

    assert factory.calls == 1, f"팩토리가 {factory.calls}번 불렸다 — 두 벌 올라갔다"
    assert runtime.vlm.available is True


# ── ③ 판독이 끝난 뒤에 닫는다 ───────────────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_unload_waits_for_the_running_read(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 추론 중인 세션을 닫으면 판독 스레드가 닫힌 모델을 만진다."""
    ask_gate = threading.Event()
    session = GateSession(ask_gate)
    reader = VlmReader(lambda: session, budget_ms=10_000)
    reader.load()
    runtime = _runtime(cfg, clock, reader, mode="factory")

    assert runtime.vlm.submit(b"jpeg", now_ms=1) is True
    assert session.asking.wait(WAIT_S)
    assert runtime.set_mode("guard") is None
    # 해제 스레드가 닫을 틈을 준다. 고치기 전 코드는 이 사이에 판독 중인 세션을 닫는다.
    session.closed.wait(0.5)
    ask_gate.set()

    assert session.closed.wait(WAIT_S), "모드를 떠났는데 VRAM 을 놓지 않았다"
    assert session.closed_while_asking is False, "판독 중인 세션을 닫았다"
    _join_profile_threads()


# ── ④ 종료가 모델을 내린다 ──────────────────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_release_unloads_the_reader(cfg: dict, clock: FakeClock) -> None:
    session = GateSession()
    reader = VlmReader(lambda: session, budget_ms=10_000)
    reader.load()
    runtime = _runtime(cfg, clock, reader, mode="factory")

    runtime.release()

    assert session.closed.is_set(), "종료했는데 VLM 을 내리지 않았다"
    assert runtime.vlm.available is False


@pytest.mark.usefixtures("unlock_modes")
def test_release_does_not_wait_out_a_load(cfg: dict, clock: FakeClock) -> None:
    """⚠️ 적재(14.5초) 도중에 끝내도 종료가 상한을 넘겨 막히지 않는다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode="guard")
    assert runtime.set_mode("factory") is None
    assert factory.entered.wait(WAIT_S)

    started = time.monotonic()
    runtime.release()
    elapsed = time.monotonic() - started

    assert elapsed < JOIN_TIMEOUT_S + 1.0, f"종료가 {elapsed:.1f}초 막혔다"
    factory.gate.set()
    _join_profile_threads()
    assert factory.sessions[0].closed.wait(WAIT_S), "적재가 끝난 뒤에도 내리지 않았다"


# ── ⑤ 공장 모드로 기동하면 올린다 ───────────────────────────


@pytest.mark.usefixtures("unlock_modes")
def test_factory_boot_starts_loading(cfg: dict, clock: FakeClock) -> None:
    """`--mode factory` 기동. `set_mode` 를 거치지 않아도 올라가야 한다."""
    factory = GateFactory()
    runtime = _runtime(cfg, clock, VlmReader(factory), mode="factory")
    assert factory.calls == 0, "생성만으로 적재가 돌면 시험마다 모델이 올라간다"

    runtime.begin(FakeSocket(clock))

    assert factory.entered.wait(WAIT_S), "공장 모드로 기동했는데 적재를 시작하지 않았다"
    factory.gate.set()
    _join_profile_threads()
    assert runtime.vlm.available is True
