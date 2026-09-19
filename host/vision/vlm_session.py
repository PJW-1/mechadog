"""VLM 세션 — 파일과 GPU 를 실제로 만지는 유일한 자리 (WBS 4.8.0 · ADR-35).

`vlm_reader.py` 가 *"이 모듈은 이것을 만들지 않는다"* 고 미뤄 둔 그 조각이다.
`detector.py` 가 세션 생성을 `session_factory` 로 밀어낸 것과 같은 경계이며, 그래서
판독기 쪽은 **가중치도 GPU 도 없이 전수 검증**된다.

⚠️ **`transformers` 를 모듈 수준에서 import 하지 않는다.** 이것을 import 하는 것만으로
2.7GB 짜리 의존성이 필요해지면, VLM 을 쓰지 않는 CI 와 개발 PC 에서 `host.runtime`
자체가 죽는다. 라이브러리가 없으면 **팩토리가 `None` 을 돌려주고 판독은 «없음» 으로
동작한다** (ADR-35 결정 6 · Tier 3).

⚠️ **가중치 경로가 설정에 없다.** Hugging Face 캐시에서 모델 ID 로 읽는다 —
샤드·인덱스·토크나이저가 한 벌로 움직여서 파일 하나를 `models/` 로 떼면 오히려 깨진다.
환경 세팅 절차는 `models/README.md` ③ 에 있다.
"""

from __future__ import annotations

import importlib.util
import io
from collections.abc import Callable, Mapping
from typing import Any

from host.common.logging_setup import event_logger

LOG = event_logger("mechadog.vision")

#: 세션을 만들려면 있어야 하는 것들. 하나라도 없으면 «없음» 으로 간다.
REQUIRED_PACKAGES = ("torch", "transformers", "torchvision", "PIL")


def missing_packages() -> tuple[str, ...]:
    """없는 의존성 목록. 비어 있으면 세션을 만들 수 있다.

    ⚠️ **`torchvision` 이 빠지면 모델이 아니라 «프로세서» 를 만들 때 터진다** —
    `Qwen2VLVideoProcessor requires the Torchvision library`. 영상을 쓰지 않는데도
    `AutoProcessor` 가 비디오 프로세서를 같이 만들기 때문이다. 그래서 여기서 함께 본다.
    """
    return tuple(name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None)


def to_image(payload: Any) -> Any:
    """판독에 넣을 것을 PIL 이미지로 맞춘다.

    런타임은 구역 프레임의 **JPEG 바이트**를 그대로 넘긴다(`result.jpeg`) — 디코드한
    배열을 넘기면 워커가 이미 한 디코드를 한 번 더 하는 셈이고, 판독은 초당 한 번이
    아니라 구역당 한 번이라 여기서 푸는 편이 싸다.
    """
    from PIL import Image

    if isinstance(payload, bytes | bytearray):
        return Image.open(io.BytesIO(bytes(payload))).convert("RGB")
    if isinstance(payload, Image.Image):
        return payload.convert("RGB")
    # numpy 배열(BGR)로 들어오는 경로도 받아 둔다 — 워커가 디코드한 프레임이다.
    import numpy as np

    if isinstance(payload, np.ndarray):
        if payload.ndim != 3 or payload.shape[2] != 3:
            raise ValueError(f"HxWx3 이미지가 필요함: shape={payload.shape}")
        return Image.fromarray(payload[:, :, ::-1])
    raise TypeError(f"판독에 넣을 수 없는 형식: {type(payload).__name__}")


class QwenVlSession:
    """`Qwen2-VL` 한 벌. **bf16 으로만 올린다.**

    ⚠️ **양자화하지 않는다.** 4bit 는 실측에서 판독이 무너졌고(ADR-35 대안 ⓐ),
    `factory` 프로파일이 ~7.1GB 로 10GB 안에 들어가므로 정밀도를 깎을 이유도 없다.
    """

    def __init__(self, model_id: str, *, max_new_tokens: int = 32) -> None:
        import torch
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        self._torch = torch
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        if self._device == "cpu":
            # 돌기는 하지만 질문당 수십 초다. 조용히 느려지면 원인을 못 찾는다.
            LOG.warning("vlm_cpu_fallback", detail="CUDA 를 못 찾아 CPU 로 올린다")
        self._model = Qwen2VLForConditionalGeneration.from_pretrained(
            model_id, dtype=torch.bfloat16, device_map=self._device
        )
        self._processor = AutoProcessor.from_pretrained(model_id)
        self._max_new_tokens = int(max_new_tokens)

    def ask(self, image: Any, prompt: str) -> str:
        """이미지 한 장에 질문 하나. **표본을 뽑지 않는다** (`do_sample=False`).

        같은 사진에 같은 질문이면 같은 답이 나와야 한다 — 판정 근거가 실행마다
        달라지면 실기 기록을 견줄 수 없다.
        """
        messages = [
            {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}
        ]
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self._processor(text=[text], images=[to_image(image)], return_tensors="pt")
        inputs = inputs.to(self._device)
        with self._torch.inference_mode():
            out = self._model.generate(
                **inputs, max_new_tokens=self._max_new_tokens, do_sample=False
            )
        trimmed = out[0][inputs["input_ids"].shape[1] :]
        return str(self._processor.decode(trimmed, skip_special_tokens=True)).strip()

    def close(self) -> None:
        """VRAM 을 놓는다. **캐시까지 비운다** — 안 비우면 다음 적재가 들어갈 자리가 없다."""
        self._model = None
        self._processor = None
        if self._device == "cuda":
            self._torch.cuda.empty_cache()


def build_session_factory(config: Mapping[str, Any]) -> Callable[[], QwenVlSession] | None:
    """설정에서 세션 팩토리를 만든다. **못 만들면 `None`** — 예외가 아니다.

    ⚠️ **여기서 모델을 올리지 않는다.** 팩토리를 돌려줄 뿐이고, 적재는 `factory` 모드에
    들어갈 때 워커 스레드가 한다(5.5초).
    """
    section = (config.get("vision") or {}).get("vlm")
    if not isinstance(section, Mapping):
        return None
    missing = missing_packages()
    if missing:
        LOG.info("vlm_dependencies_missing", missing=list(missing))
        return None
    model_id = str(section["model_id"])
    max_new_tokens = int(section.get("max_new_tokens", 32))

    def factory() -> QwenVlSession:
        return QwenVlSession(model_id, max_new_tokens=max_new_tokens)

    return factory


__all__ = [
    "REQUIRED_PACKAGES",
    "QwenVlSession",
    "build_session_factory",
    "missing_packages",
    "to_image",
]
