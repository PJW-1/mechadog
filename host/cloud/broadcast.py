"""관제 방송 TTS (WBS 4.8.2 · ADR-38).

`4.8.1` 이 사건마다 만드는 한국어 한 문장을 Host PC 스피커로 읽는다. 대시보드
자막은 같은 문장을 사건의 `judgement.sentence` 로 받아 화면에 낸다
(`host/dashboard/static/`) — 이 모듈은 **소리**만 맡는다.

로봇 스피커(MP3 모듈, `escalation.sound`)와는 다른 경로다 — 그쪽은 TF 카드의 고정 문장만
재생하고, 이쪽은 Piper 로 그 자리에서 합성한다 (ADR-38).

⚠️ `say()` 는 운용 루프 스레드에서 불리며 예외 없이 즉시 돌아온다 — 작은 큐 + 데몬 워커
스레드 하나가 합성·재생을 순서대로 한다.

관제 화면의 음량·무음 조절(`4.8.2`)은 `volume`(0~100)·`muted` 로 들어온다.
대시보드 스레드가 바꾸고 워커 스레드가 읽으므로 락으로 묶는다 — 자막은
`judgement.sentence` 경로 그대로라 무음이어도 계속 나온다.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from host.common.config import repo_path
from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.broadcast")

#: 사원증 마커와 같은 규칙(ADR-20) — 모델 가중치는 저장소에 올리지 않는다(`models/*` 는
#: `.gitignore`). 받은 모델을 저장소의 `models/piper/` 에 두면 이 기본값으로 읽힌다.
#: `config.yaml` 의 `broadcast.piper_model` 로 덮어쓰며, 상대 경로는 저장소 루트 기준이다.
DEFAULT_MODEL_PATH = str(repo_path("models/piper/ko_KR-kss-medium.onnx"))

#: 방송은 최근 사건에만 의미가 있다 — 밀리면 오래된 문장을 쌓지 않고 버린다
#: (제어 루프와 같은 "최신 우선 드롭" 원칙, CONTRIBUTING 7절 ③).
_QUEUE_MAXSIZE = 4

SynthFn = Callable[[str], "tuple[bytes, int]"]
PlayFn = Callable[[bytes, int], None]


def _default_synth(model_path: str, length_scale: float) -> SynthFn:
    """Piper 모델을 적재하고 문장 -> (PCM16LE bytes, 표본율) 함수를 돌려준다.

    `piper` 는 여기서만 임포트한다(없는 PC·CI 에서도 모듈을 읽을 수 있게).

    참고 구현 — `experiments/wonderecho-audio/voice_pipeline.py` 의
    `synth_piper`(재생 직전 16kHz 로 리샘플링하는 부분만 없다. 여기는 재생을
    `sounddevice` 로 하므로 모델 원본 표본율 그대로 재생한다).
    """
    from piper import PiperVoice, SynthesisConfig

    voice = PiperVoice.load(model_path)
    syn_config = SynthesisConfig(length_scale=length_scale)
    sample_rate = voice.config.sample_rate

    def synth(text: str) -> tuple[bytes, int]:
        pcm = bytearray()
        for chunk in voice.synthesize(text, syn_config=syn_config):
            pcm.extend(chunk.audio_int16_bytes)
        return bytes(pcm), sample_rate

    return synth


def _scale_volume(pcm: bytes, volume: int) -> bytes:
    """PCM16LE 진폭에 음량(0~100)을 곱한다. 100 이면 원본을 그대로 돌려준다.

    int16 범위로 자른다.
    """
    if volume >= 100:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * (volume / 100.0)
    scaled: bytes = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    return scaled


def _default_play(pcm: bytes, sample_rate: int) -> None:
    """`sounddevice` 로 재생한다. 오디오 출력 장치가 없으면 여기서 던진다."""
    import numpy as np
    import sounddevice as sd

    sd.play(np.frombuffer(pcm, dtype="<i2"), sample_rate)
    sd.wait()


class Broadcaster:
    """문장을 큐에 넣고 데몬 워커가 순서대로 합성·재생한다.

    모델은 시작할 때 워커 스레드가 백그라운드로 적재한다 — 첫 문장의 재생 지연을 없앤다.
    ⚠️ 적재(piper·onnxruntime)가 GIL 을 1.5초가량 쥔다 — 운용 루프 전에 `wait_ready()`
    로 끝을 기다린다.
    """

    def __init__(
        self,
        *,
        model_path: str = DEFAULT_MODEL_PATH,
        length_scale: float = 1.2,
        volume: int = 100,
        synth: SynthFn | None = None,
        play: PlayFn = _default_play,
        preload: bool = True,
    ) -> None:
        self._model_path = model_path
        self._length_scale = length_scale
        self._play = play
        self._synth_fn = synth
        self._disabled = False
        self._load_lock = threading.Lock()
        # 관제 화면의 음량·무음 조절 (`4.8.2`). 대시보드 스레드가 쓰고 워커
        # 스레드가 읽으므로 락 하나로 묶는다.
        self._volume_lock = threading.Lock()
        self._volume = max(0, min(100, int(volume)))
        self._muted = False
        self._queue: queue.Queue[str] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="broadcast-tts", daemon=True)
        self._worker.start()
        self._preload: threading.Thread | None = None
        if preload and self._synth_fn is None:
            self._preload = threading.Thread(
                target=self._ensure_synth, name="broadcast-tts-preload", daemon=True
            )
            self._preload.start()

    @property
    def volume(self) -> int:
        with self._volume_lock:
            return self._volume

    @property
    def muted(self) -> bool:
        with self._volume_lock:
            return self._muted

    def set_volume(self, volume: int) -> None:
        """0~100 밖의 값은 잘라 받는다 — 화면이 범위를 어겨도 재생기가 죽지 않는다."""
        with self._volume_lock:
            self._volume = max(0, min(100, int(volume)))

    def set_muted(self, muted: bool) -> None:
        with self._volume_lock:
            self._muted = bool(muted)

    def say(self, text: str) -> None:
        """문장을 재생 큐에 넣는다. **절대 던지지 않고 즉시 돌아온다.**

        큐가 차 있으면(재생이 밀리는 중) 새 문장을 버리고 로그만 남긴다 — 밀린
        문장을 전부 재생하면 방송이 실제 상황보다 계속 뒤처진다.
        """
        # 무음이면 큐에도 넣지 않는다. 무음 전에 이미 들어간 문장은 워커가 건너뛴다.
        if self._disabled or self.muted or not text:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            LOG.warning("broadcast_queue_full", dropped=text[:80])
        except Exception as exc:  # noqa: BLE001 — 이 함수는 절대 던지면 안 된다
            LOG.error("broadcast_say_failed", error=f"{type(exc).__name__}: {exc}")

    def wait_ready(self, timeout_s: float) -> bool:
        """미리 적재가 끝날 때까지 최대 `timeout_s` 초 기다린다. 제한 시간에 걸리면 False.

        적재 실패(piper 없음)도 끝난 것이다 — 방송만 꺼진 채 True 로 곧바로 돌아온다.
        """
        started = time.monotonic()
        if self._preload is not None:
            self._preload.join(timeout_s)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if self._preload is not None and self._preload.is_alive():
            LOG.warning("broadcast_preload_timeout", elapsed_ms=elapsed_ms, timeout_s=timeout_s)
            return False
        LOG.info("broadcast_ready", elapsed_ms=elapsed_ms, enabled=not self._disabled)
        return True

    def close(self) -> None:
        """워커 스레드를 정리한다."""
        self._stop.set()
        self._worker.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._disabled:  # 방송이 꺼진 뒤 큐에 남아 있던 문장은 조용히 버린다
                continue
            if self.muted:  # 무음이면 합성·재생을 건너뛴다 — 자막은 별개 경로라 그대로 나온다
                continue
            self._speak(text)

    def _speak(self, text: str) -> None:
        synth_fn = self._ensure_synth()
        if synth_fn is None:
            return
        try:
            pcm, sample_rate = synth_fn(text)
            # 음량 곱하기도 감싼다 — 여기서 던지면(홀수 길이 PCM) 워커 스레드가 죽어
            # 그 뒤 방송이 조용히 끊긴다.
            pcm = _scale_volume(pcm, self.volume)
        except Exception as exc:  # noqa: BLE001 — 문장 하나 실패로 워커를 죽이지 않는다
            LOG.error("broadcast_synth_failed", error=f"{type(exc).__name__}: {exc}")
            return
        try:
            self._play(pcm, sample_rate)
        except Exception as exc:  # noqa: BLE001 — 재생 실패도 다음 문장을 막으면 안 된다
            # 끄지 않는다 — 사건은 드물어 문장마다 로그 한 줄이면 되고, 장치(USB 스피커)가
            # 돌아오면 재시작 없이 다음 문장부터 다시 나온다.
            LOG.error("broadcast_play_failed", error=f"{type(exc).__name__}: {exc}")

    def _ensure_synth(self) -> SynthFn | None:
        """합성 함수를 돌려준다. 없으면 (처음 한 번만) 적재를 시도한다.

        piper 가 없거나, 모델 파일이 없거나, 그 밖의 적재 오류가 나면 **경고
        로그를 한 번 남기고 방송을 끈다** — 런타임은 계속 돌아야 한다.
        """
        with self._load_lock:
            if self._synth_fn is not None or self._disabled:
                return self._synth_fn
            try:
                self._synth_fn = _default_synth(self._model_path, self._length_scale)
            except Exception as exc:  # noqa: BLE001 — piper 부재도 여기서 걸린다
                self._disabled = True
                LOG.warning(
                    "broadcast_disabled",
                    reason="piper 또는 모델을 적재할 수 없음",
                    model_path=self._model_path,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return None
            return self._synth_fn


def from_config(cfg: Mapping[str, Any]) -> Broadcaster | None:
    """`config.yaml` 의 `broadcast` 절을 읽어 `Broadcaster` 를 만든다.

    절이 없거나 `enabled` 가 아니면 `None` — 호출부(`main()`)는 이때 방송을
    아예 두지 않는다.
    """
    section = cfg.get("broadcast")
    if not isinstance(section, Mapping) or not section.get("enabled", False):
        return None
    return Broadcaster(
        model_path=str(repo_path(section.get("piper_model", DEFAULT_MODEL_PATH))),
        length_scale=float(section.get("length_scale", 1.2)),
        volume=int(section.get("volume", 100)),
    )
