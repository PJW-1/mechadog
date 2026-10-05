"""VLM 세션 — 파일과 GPU 를 실제로 만지는 유일한 자리 (WBS 4.8.0 · ADR-35).

`vlm_reader.VlmSession` 의 실제 구현이다. `transformers` 등은 모듈 수준에서 import 하지
않는다 — 없으면 팩토리가 `None` 을 돌려주고 판독은 «없음» 으로 동작한다 (ADR-35 결정 6).

가중치는 설정 경로가 아니라 Hugging Face 캐시에서 모델 ID 로 읽는다 (세팅 절차:
`models/README.md` ③). `model_id` 자리에 LoRA 병합 모델 폴더의 절대 경로를 주면 그 폴더를
읽는다 — `from_pretrained` 가 둘 다 받는다 (`tools/vlm_lora/README.md`).
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

    `torchvision` 도 본다 — `AutoProcessor` 가 비디오 프로세서를 함께 만들며 요구한다.
    """
    return tuple(name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None)


def to_image(payload: Any) -> Any:
    """판독에 넣을 것을 PIL 이미지로 맞춘다.

    JPEG 바이트(`result.jpeg`)와 BGR numpy 배열을 받는다.
    """
    from PIL import Image

    if isinstance(payload, bytes | bytearray):
        return Image.open(io.BytesIO(bytes(payload))).convert("RGB")
    if isinstance(payload, Image.Image):
        return payload.convert("RGB")
    # numpy 배열(BGR)로 들어오는 경로.
    import numpy as np

    if isinstance(payload, np.ndarray):
        if payload.ndim != 3 or payload.shape[2] != 3:
            raise ValueError(f"HxWx3 이미지가 필요함: shape={payload.shape}")
        return Image.fromarray(payload[:, :, ::-1])
    raise TypeError(f"판독에 넣을 수 없는 형식: {type(payload).__name__}")


def chat_messages(prompt: str) -> list[dict[str, Any]]:
    """질문 하나의 채팅 메시지 — 이미지 한 장 뒤에 질문 글. LoRA 학습(`tools/vlm_lora`)도 이것을 쓴다."""
    return [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]


class QwenVlSession:
    """`Qwen2-VL` 한 벌 — bf16 으로만 올린다(양자화하지 않는다 · ADR-35 대안 ⓐ · ADR-41 결정 7)."""

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
        """이미지 한 장에 질문 하나. 결정론적 생성(`do_sample=False`)이다 — 같은 입력에 같은 답."""
        text = self._processor.apply_chat_template(
            chat_messages(prompt), tokenize=False, add_generation_prompt=True
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
        """VRAM 을 놓고 CUDA 캐시까지 비운다."""
        self._model = None
        self._processor = None
        if self._device == "cuda":
            self._torch.cuda.empty_cache()


def build_session_factory(config: Mapping[str, Any]) -> Callable[[], QwenVlSession] | None:
    """설정에서 세션 팩토리를 만든다. 못 만들면 `None`(예외 아님).

    모델을 올리지 않는다 — 적재는 기동 때 `VlmWorker` 스레드가 한다 (ADR-35 결정 5).
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
    "chat_messages",
    "missing_packages",
    "to_image",
]
