"""관제 방송 TTS 검증 (WBS 4.8.2). 실제 소리는 내지 않는다 — 합성·재생을 가짜로 준다."""

import threading
import time

import numpy as np
import yaml

from host.cloud import broadcast
from host.cloud.broadcast import Broadcaster, from_config
from host.common.config import DEFAULT_CONFIG, ROOT


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


# ── 관제 음량·무음 조절 (`4.8.2`) ────────────────────────────────


def _tone_synth(_text: str) -> tuple[bytes, int]:
    """진폭 20000 짜리 int16 PCM 10개 표본 — 음량을 곱했을 때 크기로 확인한다."""
    return np.full(10, 20000, dtype="<i2").tobytes(), 16000


def test_default_volume_is_full_and_leaves_pcm_untouched() -> None:
    played: list[bytes] = []
    broadcaster = Broadcaster(
        synth=_tone_synth, play=lambda pcm, _rate: played.append(pcm), preload=False
    )
    broadcaster.say("문장")
    _wait_until(lambda: played)
    broadcaster.close()
    assert np.frombuffer(played[0], dtype="<i2")[0] == 20000


def test_set_volume_scales_the_pcm_amplitude() -> None:
    played: list[bytes] = []
    broadcaster = Broadcaster(
        synth=_tone_synth, play=lambda pcm, _rate: played.append(pcm), preload=False
    )
    broadcaster.set_volume(50)
    broadcaster.say("문장")
    _wait_until(lambda: played)
    broadcaster.close()
    assert np.frombuffer(played[0], dtype="<i2")[0] == 10000


def test_set_volume_clamps_out_of_range_values() -> None:
    broadcaster = Broadcaster(synth=_tone_synth, play=lambda _p, _r: None, preload=False)
    broadcaster.set_volume(150)
    assert broadcaster.volume == 100
    broadcaster.set_volume(-10)
    assert broadcaster.volume == 0
    broadcaster.close()


def test_muted_skips_synthesis_and_playback() -> None:
    synthesized: list[str] = []
    played: list[bytes] = []

    def counting_synth(text: str) -> tuple[bytes, int]:
        synthesized.append(text)
        return _tone_synth(text)

    broadcaster = Broadcaster(
        synth=counting_synth, play=lambda pcm, _rate: played.append(pcm), preload=False
    )
    broadcaster.set_muted(True)
    broadcaster.say("아무 말")
    # 무음이면 큐에도 넣지 않는다 — 그래서 unmute 와 경합 없이 바로 확인된다.
    assert broadcaster._queue.empty()
    broadcaster.set_muted(False)
    broadcaster.say("확인용")
    _wait_until(lambda: played)
    broadcaster.close()
    assert synthesized == ["확인용"], "무음일 때 넣은 문장은 합성조차 되지 않아야 한다"


def test_from_config_reads_the_initial_volume(monkeypatch) -> None:
    monkeypatch.setattr(
        broadcast, "_default_synth", lambda *_a, **_k: (_ for _ in ()).throw(ImportError())
    )
    broadcaster = from_config(
        {"broadcast": {"enabled": True, "piper_model": "x.onnx", "volume": 40}}
    )
    assert broadcaster.volume == 40
    broadcaster.close()


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


def test_a_mistyped_broadcast_config_does_not_stop_the_runtime() -> None:
    """설정 오기(`length_scale: "빠르게"`)는 방송만 끄고 기동은 막지 않는다 (리뷰 지적)."""
    from host.runtime_cli import _broadcaster

    assert _broadcaster({"broadcast": {"enabled": True, "length_scale": "빠르게"}}) is None


def test_from_config_passes_the_settings_through() -> None:
    broadcaster = broadcast.from_config(
        {"broadcast": {"enabled": True, "piper_model": "x.onnx", "length_scale": 1.5, "volume": 40}}
    )
    assert broadcaster is not None
    assert (broadcaster._model_path, broadcaster._length_scale, broadcaster.volume) == (
        str(ROOT / "x.onnx"),
        1.5,
        40,
    )
    broadcaster.close()


def _no_piper(monkeypatch) -> None:
    """실제 piper 를 적재하려 들지 않도록 백그라운드 적재 경로를 막아 둔다."""
    monkeypatch.setattr(
        broadcast, "_default_synth", lambda *_a, **_k: (_ for _ in ()).throw(ImportError())
    )


def test_a_relative_piper_model_resolves_against_the_repo_root(monkeypatch) -> None:
    """설정의 상대 경로는 실행 위치(CWD)가 아니라 저장소 루트 기준이다 — 비전 모델 경로와 같다."""
    _no_piper(monkeypatch)
    broadcaster = from_config(
        {"broadcast": {"enabled": True, "piper_model": "models/piper/voice.onnx"}}
    )
    assert broadcaster is not None
    assert broadcaster._model_path == str(ROOT / "models" / "piper" / "voice.onnx")
    broadcaster.close()


def test_an_absolute_piper_model_is_kept(monkeypatch, tmp_path) -> None:
    _no_piper(monkeypatch)
    model = tmp_path / "voice.onnx"
    broadcaster = from_config({"broadcast": {"enabled": True, "piper_model": str(model)}})
    assert broadcaster is not None
    assert broadcaster._model_path == str(model)
    broadcaster.close()


def test_the_default_piper_model_lives_under_the_repo_models_folder(monkeypatch) -> None:
    """기본값·설정 파일 모두 개인 PC 절대 경로가 아니라 저장소 `models/piper/` 다."""
    _no_piper(monkeypatch)
    expected = ROOT / "models" / "piper" / "ko_KR-kss-medium.onnx"
    assert str(expected) == broadcast.DEFAULT_MODEL_PATH
    section = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))["broadcast"]
    assert section["piper_model"] == "models/piper/ko_KR-kss-medium.onnx"
    broadcaster = from_config({"broadcast": {"enabled": True}})
    assert broadcaster is not None
    assert broadcaster._model_path == str(expected)
    broadcaster.close()


def test_a_bad_pcm_does_not_kill_the_worker() -> None:
    """음량을 곱하다 실패해도(홀수 길이 PCM) 워커는 살아 다음 문장을 낸다 (리뷰 지적)."""
    played: list[bytes] = []

    def synth(text: str) -> tuple[bytes, int]:
        return (b"", 16000) if text == "깨진" else _tone_synth(text)

    broadcaster = Broadcaster(
        synth=synth, play=lambda pcm, _rate: played.append(pcm), preload=False
    )
    broadcaster.set_volume(50)
    broadcaster.say("깨진")
    broadcaster.say("정상")
    _wait_until(lambda: played)
    broadcaster.close()
    assert len(played) == 1


# ── 루프 전 적재 완료 (`wait_ready`) ─────────────────────────────


def test_wait_ready_blocks_until_the_preload_finishes(monkeypatch) -> None:
    """piper 적재가 GIL 을 쥐어 10Hz 루프를 1.5초 세운다 — 루프 전에 끝을 기다릴 수 있어야 한다."""
    release = threading.Event()

    def slow_loader(_model_path: str, _length_scale: float):
        release.wait(5.0)
        return _echo_synth

    monkeypatch.setattr(broadcast, "_default_synth", slow_loader)
    broadcaster = Broadcaster(play=lambda _pcm, _rate: None)
    assert broadcaster.wait_ready(0.05) is False, "적재 중에는 제한 시간에 걸려야 한다"
    release.set()
    assert broadcaster.wait_ready(2.0) is True
    assert broadcaster._synth_fn is _echo_synth
    broadcaster.close()


def test_wait_ready_returns_at_once_when_piper_is_missing(monkeypatch) -> None:
    """적재 실패는 기동을 막지 않는다 — 방송만 꺼진 채 곧바로 돌아온다."""

    def broken_loader(_model_path: str, _length_scale: float):
        raise ImportError("piper 없음")

    monkeypatch.setattr(broadcast, "_default_synth", broken_loader)
    broadcaster = Broadcaster(play=lambda _pcm, _rate: None)
    assert broadcaster.wait_ready(2.0) is True
    assert broadcaster._disabled
    broadcaster.close()
