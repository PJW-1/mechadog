"""관제 방송 TTS 검증 (WBS 4.8.2). 실제 소리는 내지 않는다 — 합성·재생을 가짜로 준다."""

import threading
import time

from host.cloud import broadcast
from host.cloud.broadcast import Broadcaster, from_config


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "제한 시간 안에 조건을 만족하지 못함"


def _echo_synth(text: str) -> tuple[bytes, int]:
    return text.encode(), 16000


def test_say_plays_sentences_in_order() -> None:
    played: list[str] = []
    broadcaster = Broadcaster(
        synth=_echo_synth, play=lambda pcm, _rate: played.append(pcm.decode()), preload=False
    )
    broadcaster.say("첫째")
    broadcaster.say("둘째")
    broadcaster.say("셋째")
    _wait_until(lambda: len(played) == 3)
    broadcaster.close()
    assert played == ["첫째", "둘째", "셋째"]


def test_queue_overflow_drops_the_newest_sentence() -> None:
    """워커가 재생 중일 때 큐가 차면, 넘치는 문장은 버려지고 로그만 남는다."""
    started = threading.Event()
    proceed = threading.Event()
    played: list[str] = []

    def blocking_play(pcm: bytes, _rate: int) -> None:
        started.set()
        proceed.wait(timeout=2.0)
        played.append(pcm.decode())

    broadcaster = Broadcaster(synth=_echo_synth, play=blocking_play, preload=False)
    broadcaster.say("0")  # 워커가 곧바로 집어 재생 중(blocking_play 안에서 대기)이 된다
    assert started.wait(timeout=1.0), "워커가 첫 문장을 집어 재생을 시작하지 않음"
    for i in range(1, 1 + broadcast._QUEUE_MAXSIZE):
        broadcaster.say(str(i))  # 큐를 정원까지 채운다
    broadcaster.say("overflow")  # 정원을 넘으므로 버려져야 한다
    proceed.set()
    _wait_until(lambda: len(played) == 1 + broadcast._QUEUE_MAXSIZE)
    broadcaster.close()
    assert "overflow" not in played


def test_continues_after_synth_and_play_exceptions() -> None:
    played: list[str] = []

    def flaky_synth(text: str) -> tuple[bytes, int]:
        if text == "합성실패":
            raise RuntimeError("boom")
        return text.encode(), 16000

    def flaky_play(pcm: bytes, _rate: int) -> None:
        if pcm == "재생실패".encode():
            raise RuntimeError("boom")
        played.append(pcm.decode())

    broadcaster = Broadcaster(synth=flaky_synth, play=flaky_play, preload=False)
    broadcaster.say("합성실패")
    broadcaster.say("재생실패")
    broadcaster.say("정상")
    _wait_until(lambda: "정상" in played)
    broadcaster.close()
    # 예외 두 건(합성 하나 · 재생 하나)을 지나 마지막 문장은 재생됐다 — 워커가 죽지 않았다.
    assert played == ["정상"]


def test_say_never_blocks_even_with_a_slow_player() -> None:
    def slow_play(_pcm: bytes, _rate: int) -> None:
        time.sleep(1.0)

    broadcaster = Broadcaster(synth=_echo_synth, play=slow_play, preload=False)
    start = time.monotonic()
    for i in range(10):
        broadcaster.say(str(i))
    elapsed = time.monotonic() - start
    broadcaster.close()
    assert elapsed < 0.5, f"say() 가 {elapsed:.2f}s 나 걸림 — 10Hz 제어 루프를 막는다"


def test_say_never_raises_even_when_disabled() -> None:
    broadcaster = Broadcaster(synth=_echo_synth, play=lambda _pcm, _rate: None, preload=False)
    broadcaster._disabled = True
    broadcaster.say("아무 일도 안 일어나야 함")  # 예외를 던지면 안 된다
    broadcaster.close()


def test_close_stops_the_worker_thread() -> None:
    broadcaster = Broadcaster(synth=_echo_synth, play=lambda _pcm, _rate: None, preload=False)
    broadcaster.close()
    assert not broadcaster._worker.is_alive()


def test_missing_piper_or_model_disables_broadcast_without_raising(monkeypatch) -> None:
    """piper 가 없거나 모델 파일이 없으면 경고 로그 한 번을 남기고 방송을 끈다."""

    def broken_loader(_model_path: str, _length_scale: float):
        raise ImportError("piper 없음 또는 모델 없음")

    monkeypatch.setattr(broadcast, "_default_synth", broken_loader)
    broadcaster = Broadcaster(preload=False)
    broadcaster.say("문장")  # 절대 던지지 않는다
    _wait_until(lambda: broadcaster._disabled)
    broadcaster.close()


def test_from_config_returns_none_when_disabled_or_absent() -> None:
    assert from_config({}) is None
    assert from_config({"broadcast": {"enabled": False}}) is None


def test_from_config_builds_a_broadcaster_when_enabled(monkeypatch) -> None:
    # 실제 piper 를 적재하려 들지 않도록 백그라운드 적재 경로를 막아 둔다.
    monkeypatch.setattr(
        broadcast, "_default_synth", lambda *_a, **_k: (_ for _ in ()).throw(ImportError())
    )
    broadcaster = from_config(
        {"broadcast": {"enabled": True, "piper_model": "x.onnx", "length_scale": 1.1}}
    )
    assert isinstance(broadcaster, Broadcaster)
    broadcaster.close()
