#!/usr/bin/env bash
# v3 학습 완료 감지 → ONNX export → 형상 검증 → 목업 테스트
# 실행: bash finish_v3.sh (로그: run-logs/finish_v3.log)
set -u
cd /c/dev/mechadog-sim-view
PY=/c/dev/yolox-venv/Scripts/python.exe
LOG=run-logs/finish_v3.log
CKPT=YOLOX_outputs/ppe_yolox_nano_v3/best_ckpt.pth
FIELD=/c/dev/mechadog-field

echo "[$(date '+%H:%M:%S')] watcher start" >> "$LOG"

# 학습 프로세스(ppe_exp.py) 종료까지 대기 — 10분 간격 폴링
while true; do
  RUNNING=$(powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object { \$_.CommandLine -like '*ppe_exp.py*' } | Measure-Object | Select -ExpandProperty Count" 2>/dev/null | tr -d '\r ')
  if [ "$RUNNING" = "0" ] && [ -f "$CKPT" ]; then
    echo "[$(date '+%H:%M:%S')] training process ended, ckpt present" >> "$LOG"
    break
  fi
  sleep 600
done

# 종료 직후 파일 잠금/플러시 여유 + 최종 평가 지표 보존
sleep 60
tail -40 run-logs/train_ppe_v3.log >> "$LOG" 2>&1

# 1) ONNX export
echo "[$(date '+%H:%M:%S')] exporting onnx" >> "$LOG"
cd /c/dev/YOLOX
"$PY" tools/export_onnx.py \
  --output-name /c/dev/mechadog-sim-view/models/ppe_v3.onnx \
  -f /c/dev/mechadog-sim-view/ppe_exp.py \
  -c /c/dev/mechadog-sim-view/$CKPT >> /c/dev/mechadog-sim-view/$LOG 2>&1
cd /c/dev/mechadog-sim-view

# 2) 형상·해시·추론 검증
echo "[$(date '+%H:%M:%S')] verifying onnx" >> "$LOG"
"$PY" - <<'EOF' >> "$LOG" 2>&1
import onnxruntime as ort, hashlib, os, numpy as np
p = "C:/dev/mechadog-sim-view/models/ppe_v3.onnx"
print("size:", os.path.getsize(p))
print("sha256:", hashlib.sha256(open(p,'rb').read()).hexdigest())
s = ort.InferenceSession(p, providers=["CPUExecutionProvider"])
print("in:", [(i.name, i.shape) for i in s.get_inputs()])
print("out:", [(o.name, o.shape) for o in s.get_outputs()])
o = s.run(None, {s.get_inputs()[0].name: np.zeros((1,3,640,640), np.float32)})
print("infer ok:", o[0].shape)
EOF

# 3) models/ppe.onnx 교체 (sim-view + field) 후 목업 테스트 — field에 live_check가 있다
cp -f models/ppe_v3.onnx models/ppe.onnx
cp -f models/ppe_v3.onnx "$FIELD/models/ppe.onnx"
echo "[$(date '+%H:%M:%S')] mock test: fallen session" >> "$LOG"
cd "$FIELD"
"$PY" tools/ppe_live_check.py --device mechdog-01 \
  --images "C:/dev/mechadog-sim-view/datasets/ppe/images/train/sim_fallen_t15b" \
  --report /c/dev/mechadog-sim-view/run-logs/v3_mock_fallen.md \
  --session /c/dev/mechadog-sim-view/run-logs/v3_mock_fallen.json >> /c/dev/mechadog-sim-view/$LOG 2>&1
echo "[$(date '+%H:%M:%S')] mock test: merged val" >> "$LOG"
"$PY" tools/ppe_live_check.py --device mechdog-01 \
  --images "C:/dev/mechadog-sim-view/ppe_coco_v3/val" \
  --report /c/dev/mechadog-sim-view/run-logs/v3_mock_val.md \
  --session /c/dev/mechadog-sim-view/run-logs/v3_mock_val.json >> /c/dev/mechadog-sim-view/$LOG 2>&1

echo "[$(date '+%H:%M:%S')] ALL DONE" >> "$LOG"
