# 사례 10. 모델은 맞는데 추론 환경이 달라 «빈 결과» 가 나온 사례

- 관련 결정: [ADR-13](../DECISIONS.md#adr-13), [ADR-24](../DECISIONS.md#adr-24)
- 기록: [3.7.2 보고서 6절](../../field_tests/results/20260918_3.7.2-ppe-model/report.md), [PPE 학습 기록 2.4 · 3.8](../internal/PPE_TRAIN.md), [CONTRIBUTING 8절](../../CONTRIBUTING.md), [ENGINEERING_GUIDE «같은 코드, 같은 모델 파일, 다른 환경»](../ENGINEERING_GUIDE.md)
- 코드: [`providers.py`](../../host/vision/providers.py), [`export_ppe.py`](../../tools/ppe/export_ppe.py), [`ppe_live_check.py`](../../tools/ppe/ppe_live_check.py)

## 초기 가설

학습한 가중치를 ONNX 로 내보내고 parity 시험을 통과하면, 어느 PC 에서 onnxruntime 으로 돌려도 같은 결과가 나온다고 봤습니다. 설정 `vision.providers` 는 DirectML 을 먼저, CPU 를 뒤에 둡니다([`config.yaml`](../../config/config.yaml)). 실행 프로바이더(EP)는 «속도» 를 고르는 설정이지 «결과» 를 바꾸는 설정이 아니라는 전제였습니다.

## 관측된 실패

세 번 다른 모양으로 나타났습니다.

| 언제 | 무엇이 어긋났나 | 겉보기 증상 |
| :--- | :--- | :--- |
| 범용 검출기 도입(WBS 3.3.1) | `onnxruntime` 과 `onnxruntime-directml` 이 함께 설치돼 DirectML 이 목록에서 사라짐 | 오류 없이 CPU 로 돎. 추론 61.9ms(교체 후 8.2ms) |
| 범용 검출기 도입(WBS 3.3.1) | 전처리를 관례대로 `/255` 정규화 | 잡음 프레임에서는 정상과 똑같이 0건. 실제 사진에서는 검출이 전부 사라짐 |
| PPE 모델 3.7.2(2026-09-18) | 개발 PC 의 DirectML EP | 검출을 전부 비워 내보냄. 같은 사진에서 CPU 는 person 0.5~0.93 을 정상 검출 |

출처는 [CONTRIBUTING](../../CONTRIBUTING.md), [WBS 3.3.1](../internal/WBS.md), [ADR-24](../DECISIONS.md#adr-24), [PPE_TRAIN.md 2.4 · 3.8](../internal/PPE_TRAIN.md), [3.7.2 보고서 6절](../../field_tests/results/20260918_3.7.2-ppe-model/report.md) 입니다.

## 근거 데이터

- **세 경우 모두 예외가 나지 않았습니다.** 학습 기록은 «오류가 아니라 빈 결과로 나오므로 카메라 문제로 오인하기 쉽다» 고 적었습니다([PPE_TRAIN.md 3.8](../internal/PPE_TRAIN.md)).
- **잡음 시험은 고장을 구분하지 못했습니다.** `/255` 로 일부러 고장낸 전처리도 잡음 입력에서 정상과 똑같이 0건을 냈습니다([ADR-24](../DECISIONS.md#adr-24)).
- **parity 는 CPU EP 만 봅니다.** 내보내기 도구는 parity 를 `providers=["CPUExecutionProvider"]` 로만 재고, 메타데이터의 `runtime` 에도 «onnxruntime … CPU» 를 적습니다. 허용 오차는 `PARITY_TOL = 1e-3` 입니다([`export_ppe.py`](../../tools/ppe/export_ppe.py)). 3.7.2 에서 DirectML 이 비워 낸 결과는 이 검사 범위 밖이었습니다.

## 원인

학습 → 내보내기 → 운용 사이의 계약 중 일부만 기계로 검사되고 있었습니다. 모델 파일과 출력 모양은 parity 로 묶였지만, «전처리 규약» 과 «어느 EP 로 도는가» 는 사람의 기억에 맡겨져 있었습니다. 3.7.2 의 DirectML 빈 출력은 근본 원인을 밝히지 못했습니다. 그 PC 만 CPU 로 돌리도록 개체 로컬 설정에 기록하고 넘어갔습니다.

## 변경

1. **전처리를 계약 문자열로 박았습니다.** `PREPROCESS` 는 «BGR · 0~255 그대로 · letterbox 좌상단 정렬 · 여백 114 · 리사이즈는 버림 · INTER_LINEAR» 를 적고 메타데이터에 함께 남깁니다. 출력 모양 `[1, 8400, 9]` 과 opset 11 도 도구가 고정합니다([`export_ppe.py`](../../tools/ppe/export_ppe.py)). 규약은 원본 YOLOX `preproc()` 와 한 줄씩 대조해 정했고, 그 과정에서 반올림을 버림으로 고쳤습니다([ADR-24](../DECISIONS.md#adr-24)).
2. **EP 선택을 순수 함수로 만들고 기동 때 기록합니다.** `select_providers()` 는 선호 순서와 실제 사용 가능 목록의 교집합을 취하고 CPU 를 항상 뒤에 붙입니다. `log_selection()` 은 설정이 GPU 를 먼저 뒀는데 CPU 로 떨어지면 `execution_provider_cpu_only` 를 WARNING 으로 남깁니다([`providers.py`](../../host/vision/providers.py)). 실기 런타임 로그에는 `execution_provider DmlExecutionProvider` 가 찍힙니다([3.5.4 재검증](../../field_tests/results/20260918_3.5.4-recheck/summary.md)).
3. **설치 절차에 함정을 적었습니다.** Windows 는 `onnxruntime-directml` 하나만 두고, `get_available_providers()` 로 `DmlExecutionProvider` 를 확인합니다([CONTRIBUTING](../../CONTRIBUTING.md)).
4. **정확도는 실제 사진으로 봅니다.** 내보내기 도구의 `verify` 는 운용 `Detector` 로 실제 이미지를 돌려 박스가 나온 장수를 `host_check` 에 남기고, 하나도 없으면 실패로 끝납니다([`export_ppe.py`](../../tools/ppe/export_ppe.py)).

## 동일 조건 재평가

DirectML 쪽은 같은 조건 재평가가 없습니다. 3.7.2 의 개발 PC 에서 DirectML 빈 출력이 다시 나는지, 무엇 때문인지는 기록을 찾지 못했습니다.

설치 충돌 쪽은 같은 기준 PC 에서 패키지를 바로잡은 뒤 61.9ms 가 8.2ms 로 바뀌었습니다(VGA, n=50, [WBS 3.3.1](../internal/WBS.md)). 이후 ppe-v5 의 2026-10-06 XIAO 실측은 20.96fps 로 돌았습니다([2026-10-06 XIAO 실측](../../field_tests/results/20261006_ppe-xiao/summary.md)).

## 남은 한계

- **2026-10-06 기록에는 «실제로 쓴 EP» 가 남지 않았습니다.** 당시 `ppe_live_check.py` 는 `summary.md` 와 `session.json` 에 설정값 `config['vision']['providers']` 만 적었습니다. 그 기록의 «프로바이더: ['DmlExecutionProvider', 'CPUExecutionProvider']» 는 선호 목록이지 선택 결과가 아니어서, 그 런이 DirectML 로 돌았는지는 기록만으로는 확인되지 않고 이 기록은 고치지 않았습니다. 이후 기록부터는 `session.json` 의 `models`(`{name, sha256, provider}`, 블랙박스 meta.json 과 같은 모양)와 `summary.md` 의 «실제 실행 장치» 줄에 `Detector.model_summary()` 가 돌려준 실제 장치가 남고, 선호 목록 첫 값이 GPU 인데 실제가 CPU 이면 경고 한 줄이 붙습니다. 이 키가 없는 옛 `session.json` 은 읽는 도구가 그대로 처리합니다.
- **`host_check` 도 EP 를 적지 않습니다.** 박스가 나온 장수만 기록하므로, DirectML 이 비워 냈는지 CPU 로 떨어졌는지를 사후에 가를 수 없습니다.
- parity 는 CPU EP 기준이라, DirectML 에서 원시 출력이 허용 오차 안에 드는지는 검사하지 않습니다.
- DirectML 빈 출력의 근본 원인은 밝히지 못했습니다.
