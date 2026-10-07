# ONNX 정밀도 비교와 채택 결정 (2026-10-06 측정)

coco·ppe 모델을 FP16·INT8 동적·INT8 정적(QDQ)으로 바꿔 FP32 와 같은 입력에서 비교한 기록이다. 도구는
`tools/bench/precision_bench.py`(#435, #442)이고, 로봇·XIAO 에 접속하지 않고 저장된 세션 프레임으로 쟀다.
원자료는 이 폴더의 `accuracy.dml.json`·`latency.json`·`calibration.json` 이다.

## 결론

- **운영 정밀도는 FP32 를 유지한다(사용자 결정 2026-10-07).** `config/config.yaml` 의 `model_path` 는 바꾸지
  않는다.
- **INT8 은 기각한다.** 동적 INT8 은 DirectML 에서 FP32 보다 느리고(PPE 9.2ms 대 6.3ms) PPE 박스 일치가
  71.7% 다. 정적 INT8(QDQ)은 CPU 에서 가장 빠르지만(PPE 58.0ms 대 96.6ms) PPE 모델이 박스를 하나도 내지
  않았다(0/2,265).
- **FP16 은 후보로 남긴다.** 크기가 절반이고 DirectML 모델 단독 추론이 4.0ms 에서 2.6ms 로 줄며, 박스가
  FP32 와 99.9% 일치한다. 채택하지 않은 이유는 [아래](#fp16-을-채택하지-않은-이유)에 적었다.

## 측정 조건

- **입력**: `20261006_ppe-xiao` 세션 8,499프레임. INT8 정적 보정에 쓴 100장(구간 버튼 밖에서 사람이 잡힌
  이벤트를 프레임 번호순으로 세워 등간격으로 고름)을 빼고, 8,399프레임으로 일치를 쟀다.
- **기준 모델**: FP32 는 배포 모델이다. PPE 는 ppe-v5(sha256 `145c2dc9…790e2`), coco 는 `c5c2d13e…8063` 이다.
- **일치 기준**: coco 사람 박스는 같은 라벨·IoU 0.5 이상·점수순 1:1 로 맞춘다. PPE 는 FP32 사람 박스로 자른
  같은 크롭(여유 0.08)에 정밀도별 모델을 넣어 맞춘다.
- **판정 상태**: 프레임마다 `ppe_live_check.judge` 판정 목록이 FP32 와 같은지 센다.
- **지연**: 같은 50장을 warmup 20회 뒤 200회 돌렸다. 조합마다 새 프로세스를 띄웠다. `pipeline` 은
  전처리·후처리(NMS 는 개체 설정 그대로)를 포함한 `Detector.detect` 시간이고, `run` 은 모델 단독 시간이다.
- **실행 장치**: DirectML(`[Dml, CPU]`)과 CPU 단독. 정확도는 DirectML 에서만 쟀다.
- **예비 값**: GPU 를 다른 작업과 함께 쓸 수 있는 상태에서 쟀다.

## 결과

### 크기와 FP32 일치 (DirectML, 8,399프레임)

| 정밀도 | 크기 MiB (coco / ppe) | coco 사람 박스 일치 | PPE 박스 일치 | 판정 상태가 FP32 와 같은 프레임 |
|---|---|---|---|---|
| FP32 | 34.2 / 34.1 | 7,825/7,825 (기준) | 2,265/2,265 (기준) | 8,399/8,399 |
| FP16 | 17.1 / 17.1 | 7,822 (99.96%), 누락 3 · 추가 2 | 2,263 (99.91%), 누락 2 · 추가 21 | 8,380 |
| INT8 동적 | 8.7 / 8.8 | 7,299 (93.3%), 누락 526 · 추가 35 | 1,624 (71.7%), 누락 641 · 추가 334 | 7,748 |
| INT8 QDQ | 8.9 / 9.0 | 7,758 (99.1%), 누락 67 · 추가 50 | 0 (0%), 누락 2,265 | 8,187 |

### 지연 ms (평균 / P95)

| 모델 · 정밀도 | DirectML pipeline | DirectML run | CPU pipeline | CPU run |
|---|---|---|---|---|
| coco FP32 | 8.5 / 9.8 | 5.1 / 5.8 | 83.2 / 101.9 | 82.6 / 104.2 |
| coco FP16 | **7.3 / 8.7** | **3.4 / 3.9** | 87.4 / 111.8 | 86.2 / 139.4 |
| coco INT8 동적 | 11.0 / 12.4 | 7.6 / 8.2 | 152.7 / 216.8 | 129.8 / 173.3 |
| coco INT8 QDQ | 11.6 / 12.7 | 8.0 / 8.6 | **51.2 / 67.8** | **54.3 / 72.5** |
| ppe FP32 | 6.3 / 7.1 | 4.0 / 4.4 | 96.6 / 133.3 | 95.0 / 126.1 |
| ppe FP16 | **5.2 / 6.2** | **2.6 / 3.2** | 99.9 / 143.0 | 89.4 / 118.7 |
| ppe INT8 동적 | 9.2 / 10.1 | 6.4 / 7.0 | 158.8 / 217.5 | 151.0 / 203.4 |
| ppe INT8 QDQ | 10.5 / 11.8 | 7.4 / 8.3 | **58.0 / 75.2** | **51.3 / 68.9** |

DirectML 에서는 FP16 만 FP32 보다 빨랐다. CPU 의 pipeline 에서는 INT8 QDQ 만 빨랐고 FP16 은 조금 느렸다
(coco 87.4 대 83.2ms, ppe 99.9 대 96.6ms). CPU 의 모델 단독(`run`)에서는 ppe FP16 이 FP32 보다 빨랐지만
(89.4 대 95.0ms) coco FP16 은 느렸으므로(86.2 대 82.6ms), CPU 에서 FP16 이 빠르다고 할 근거는 없다.

## FP16 을 채택하지 않은 이유

- **이득이 작다.** 전처리를 포함하면 모델당 약 1ms 다(coco 8.5 → 7.3ms, ppe 6.3 → 5.2ms).
- **실행 장치별로 정밀도를 고를 수 없다.** `model_path` 는 검출기 절마다 하나이고, `vision.providers` 는 모든
  검출기가 함께 쓰며 CPU 가 항상 마지막에 붙는다(`host/vision/providers.py`, `host/common/config.py`).
  FP16 으로 바꾸면 DirectML 을 쓸 수 없을 때 CPU 도 FP16 을 돌린다. CPU 에서는 FP16 이 빠르다는 근거가 없고
  (pipeline 은 coco·ppe 모두 FP32 보다 느렸다), CPU FP16 의 정확도는 재지 않았다.
- **배포 검증 체계를 바꿔야 한다.** `tests/test_model_info.py` 는 설정이 가리키는 모델마다 메타 파일이 있고
  그 해시가 `tools/fetch_models.py` 의 `WEIGHTS` 와 같기를 요구한다. 로컬에서 변환한 파일은 이 목록에 없으므로,
  채택하려면 FP16 파일을 Release 로 배포해 목록에 넣어야 한다.
- **착용자 쪽 근거가 없다.** FP16 은 PPE 박스를 21개 더 냈다. 이 박스가 정상 착용자를 위반으로 읽게
  만드는지는 원본 착용 프레임이 없어 잴 수 없다. 원본이 있는 [후면 세션](../20261006_ppe-xiao-rear/ablation.md)은
  위반 구간만 담고 있다.

다시 검토할 시점은 GPU 여유가 모자라 프레임을 놓치기 시작할 때다. 그때는 장치별 모델 경로, 배포 목록,
착용자 프레임 비교를 함께 처리한다.

## 한계

- **입력 프레임은 판정을 덧그린 이미지다.** `--save-dir` 로 남긴 프레임이라 판정 테두리와 글자가 남아 있다
  ([낮 ablation](../20261006_ppe-xiao/ablation.md)). 그래서 FP32 로 다시 추론해도 판정 가능률이 0.56% 이고,
  실기 기록과 판정이 같은 프레임은 2,823/8,499 다(`accuracy.dml.json` 의 `rates`·`states_vs_live`).
  따라서 «판정 상태가 같다» 는 대부분 확인불가끼리의 일치이고, 박스 일치와 추가 검출 수는 이 이미지 분포 위의
  값이다. 정밀도끼리 같은 입력을 보므로 비교 자체는 성립한다.
- **PPE 일치는 FP32 사람 박스의 크롭 위에서 쟀다.** 정밀도마다 사람 박스가 달라져 크롭이 바뀌는 영향은 판정
  상태 비교에만 나타난다.
- **판정은 `ppe_live_check.judge` 기준이다.** 런타임 `PpeDetector` 와는 머리 클리핑 좌표와 누적 해제 규칙이
  다르다.
- **PPE 지연은 풀프레임 입력으로 쟀다.** 런타임은 사람 크롭을 넣으므로 절대값은 다르고, 정밀도끼리의 비교에만
  뜻이 있다.
- **C1~C3 는 비교에 쓰지 않았다.** 이 입력에서는 FP32 도 C2 를 통과하지 못한다(`accuracy.dml.json` 의
  `acceptance`).
- **INT8 QDQ 의 PPE 박스 0개는 원인을 밝히지 못했다.**
- **재지 않은 것**: PyTorch FP32, VRAM, 처리량 FPS, JPEG 수신부터 최종 판정까지의 시간.

## 재현

세션 프레임(`frames/`)은 얼굴이 찍혀 저장소에 없다. 같은 프레임이 있는 PC 에서 세션을 찍은 개체 프로파일로
돌린다. 변환 모델은 깃이 무시하는 `models/precision/` 에 생긴다.

```powershell
$S = "field_tests/results/20261006_ppe-xiao"
python tools/bench/precision_bench.py convert  --device mechdog-01 --frames "$S/frames" --session "$S/session.json"
python tools/bench/precision_bench.py accuracy --device mechdog-01 --frames "$S/frames" --session "$S/session.json" --out models/precision/accuracy.dml.json
python tools/bench/precision_bench.py latency  --device mechdog-01 --frames "$S/frames" --session "$S/session.json" --out models/precision/latency.json
```
