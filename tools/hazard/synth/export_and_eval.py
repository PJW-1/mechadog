"""학습 체크포인트를 런타임 형식 ONNX(원시 [1,8400,7])로 내보내고, 로봇 실물 사진에서 기존 모델과 비교한다.

<yolox venv>/Scripts/python.exe export_and_eval.py <best_ckpt.pth>
"""

import inspect
import sys
from pathlib import Path

import numpy as np
import torch
from torch import nn
from yolox.exp import get_exp
from yolox.models.network_blocks import SiLU
from yolox.utils import replace_module

HERE = Path(__file__).parent
ckpt_path = Path(sys.argv[1])
out = HERE / "out" / "hazard.onnx"
out.parent.mkdir(exist_ok=True)
exp = get_exp(str(HERE / "exp_hazard_synth.py"), None)
model = exp.get_model()
ck = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
model.load_state_dict(ck.get("model", ck))
model.eval()
model = replace_module(model, nn.SiLU, SiLU)
model.head.decode_in_inference = False
extra = {"dynamo": False} if "dynamo" in inspect.signature(torch.onnx.export).parameters else {}
torch.onnx.export(
    model,
    torch.zeros(1, 3, 640, 640),
    str(out),
    input_names=["images"],
    output_names=["output"],
    opset_version=11,
    **extra,
)
import onnxruntime as ort  # noqa: E402 — 내보낸 뒤에만 필요

s = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
print("onnx", out, s.run(None, {"images": np.zeros((1, 3, 640, 640), np.float32)})[0].shape)
