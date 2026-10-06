# PPE 데이터 카드 — ppe-v5

배포 모델 `models/ppe.onnx`(ppe-v5)를 무엇으로 학습하고 무엇으로 평가했는지 한곳에 모은 문서입니다. 수치마다 원문을 링크하고, 원문에서 확인하지 못한 값은 «확인 못 함» 으로 적습니다. 학습 데이터 원본과 빌드 산출물은 저장소에 없으므로(이미지 재배포 금지·얼굴 포함), 이 카드는 저장소 안의 기록과 도구만으로 썼습니다.

- 고지·라이선스 원문: [models/NOTICE](../models/NOTICE)
- 학습 기록: [`config/config.yaml` 의 `vision.ppe` 주석](../config/config.yaml), [PPE 학습 기록](internal/PPE_TRAIN.md)(첫 모델 YOLOX-Nano 시절)
- 실기 평가: [PPE 실기 검수](PPE_ACCEPTANCE.md), [2026-10-06 XIAO 실측](../field_tests/results/20261006_ppe-xiao/summary.md)
- 관련 사례: [사례 5 도메인 차이](case-studies/05-ppe-domain-gap.md), [사례 6 부분 라벨](case-studies/06-partial-label-background.md)

## 1. 모델

| 항목 | 값 | 출처 |
| :--- | :--- | :--- |
| 이름 | ppe-v5 (Release 태그 `ppe-v5`) | [NOTICE](../models/NOTICE) |
| 파일 | `models/ppe.onnx` · 35,779,654 바이트 · SHA-256 `145c2dc9dffc622f51e300937939ef3cf57e33fb7f42a7efa6751e28fdc790e2` | [NOTICE](../models/NOTICE) |
| 구조 | YOLOX-S · 입력 640×640 · 원시 출력 `[1, 8400, 9]`(격자 디코드 전) | [NOTICE](../models/NOTICE), [`export_ppe.py`](../tools/ppe/export_ppe.py) |
| 입력 | 사람 크롭 한 장. 사람은 1단 COCO 검출기가 찾고([ADR-12](DECISIONS.md#adr-12)) PPE 모델은 그 크롭만 봅니다 | [`rf100_prepare.py`](../tools/ppe/rf100_prepare.py) |
| 전처리 | BGR · 0~255 그대로(정규화 없음) · letterbox 좌상단 정렬 · 여백 114 · 리사이즈 버림 · INTER_LINEAR | [`export_ppe.py`](../tools/ppe/export_ppe.py) `PREPROCESS` |
| 계보 | COCO 사전학습 `yolox_s.pth` → ppe-v4(2026-09-29) → ppe-v5(2026-10-02, v4 에서 미세조정) | [`config.yaml`](../config/config.yaml), [NOTICE](../models/NOTICE) |
| 라이선스 | 가중치 CC BY 4.0. 공개 학습 데이터 두 개가 모두 CC BY 4.0 이고, 비상업(NC) 데이터는 쓰지 않았습니다 | [NOTICE](../models/NOTICE) |

## 2. 클래스와 라벨 규칙

클래스는 `helmet` · `no_helmet` · `vest` · `no_vest` 넷입니다. `person` 은 학습하지 않습니다.

1. `helmet` 은 머리에 쓴 안전모, `no_helmet` 은 안전모를 쓰지 않은 머리에 칩니다.
2. `vest` 는 몸통에 걸친 조끼, `no_vest` 는 조끼 없는 몸통에 칩니다.
3. `no_*` 는 해당 물건이 없는 신체 부위에 칩니다. 배경에 치면 빈 곳이 위반으로 학습됩니다([PPE_TRAIN.md 1.5](internal/PPE_TRAIN.md)).
4. **머리 라벨이나 몸통 라벨이 빠진 사람은 학습에서 뺍니다.** 라벨 없는 맨머리·몸통이 배경으로 학습되지 않게 하려는 규칙입니다([`rf100_prepare.py`](../tools/ppe/rf100_prepare.py) `person_label_problem`, 사례 6).
5. 원본 범주 이름은 매핑표로 4클래스에 맞추고, 표에 없는 이름이 나오면 변환을 멈춥니다(`MAPPING`).
6. 사람마다 런타임과 같은 식으로 크롭합니다. 여유는 검증·시험이 런타임 값 0.08, 학습이 0.0~0.2 무작위입니다(`RUNTIME_PAD`, `TRAIN_PAD_RANGE`).

## 3. 출처와 라이선스

| # | 데이터 | 라이선스 | 쓰임 | 비고 |
| :-- | :--- | :--- | :--- | :--- |
| 1 | Roboflow Universe «construction safety» (Roboflow 100), <https://universe.roboflow.com/j2-p/construction-safety-gsnvb-h1yhi> | CC BY 4.0 | v4·v5 학습·검증·시험 | 원 분할 train/valid/test 를 우리 train/val/test 로 씁니다 |
| 2 | Mendeley PPE2286 v6 (Huang·Cheng), DOI 10.17632/zkzghjvpn2.6 | CC BY 4.0 | v4·v5 학습·검증, Mendeley 전용 시험 | 사람 라벨이 없어 런타임 COCO 검출기로 사람 박스를 만들었습니다. 1번과 같은 사진(좌우 반전 포함)은 dHash 로 뺐습니다 |
| 3 | 하단 각도 자체 촬영 — 안전모·조끼를 착용한 팀원 1명을 낮은 시점에서 360도 돌며 찍은 영상 프레임 | 팀 산출물(배포하지 않음) | v5 미세조정 학습·검증 | 400프레임 중 121프레임 제외, 279프레임 사용. Grounding DINO(Apache-2.0) 자동 라벨. 폰 사진을 XIAO 긴 변 640 으로 줄였습니다 |

출처: [models/NOTICE](../models/NOTICE) §3, [`mendeley_prepare.py`](../tools/ppe/mendeley_prepare.py), [`colab_ppe_v5_lowangle.ipynb`](../tools/ppe/colab_ppe_v5_lowangle.ipynb).

첫 모델(YOLOX-Nano, 3.7.2)은 위와 다른 Roboflow 공개 세트 4개를 병합한 데이터(3,219장)로 학습했습니다. 그 데이터는 v4·v5 의 학습에 쓰이지 않습니다([PPE_TRAIN.md 1.1](internal/PPE_TRAIN.md)) [추측: v4 의 `ppe4_cs_*` 빌드 도구가 construction-safety 와 Mendeley 만 읽으므로].

## 4. 분할과 크기

v4 의 데이터 빌드 이름은 `ppe4_cs_md_v2` 이고, v5 는 여기에 하단 각도 279프레임을 더했습니다.

| 분할 | 구성 | 크기 | 출처 |
| :--- | :--- | :--- | :--- |
| train | Roboflow train + Mendeley train (+ v5 하단 각도 train 구간) | v4: 사람 크롭 2,255개, 그중 `no_helmet` 박스 346개. v5 추가분 크롭 수는 확인 못 함 | [`config.yaml`](../config/config.yaml) |
| val | Roboflow valid + Mendeley valid 의 절반(시드로 분할) | 확인 못 함 | [`mendeley_prepare.py`](../tools/ppe/mendeley_prepare.py) |
| test (Roboflow) | Roboflow test 그대로(`instances_test.json`) — v1 과 같은 잣대로 비교하려고 고정 | 크롭 수는 확인 못 함. 운용 경로 목업은 이 세트의 173명으로 돌렸습니다 | [PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md) |
| test (Mendeley) | Mendeley valid 의 나머지 절반(`instances_test_md.json`) | 확인 못 함 | [`mendeley_prepare.py`](../tools/ppe/mendeley_prepare.py) |
| v5 하단 각도 val | 400프레임을 촬영 순서로 40장씩 10구간으로 나눠 3·8번째 구간(`VAL_BLOCKS = {2, 7}`)을 검증으로 씀(`instances_val_low.json`) | 착용자 58명(PR #385). 제외 뒤 프레임 수는 확인 못 함 | [`colab_ppe_v5_lowangle.ipynb`](../tools/ppe/colab_ppe_v5_lowangle.ipynb) |
| v5 best 선택용 val | 기존 val + 하단 각도 val(`instances_val_mix.json`) | 확인 못 함 | 같은 노트북 |

분할 누수 방지 규칙은 이렇습니다. Roboflow 변환은 test → val → train 순서로 처리해 같은 사진이 겹치면 평가 쪽이 가져갑니다([`rf100_prepare.py`](../tools/ppe/rf100_prepare.py) `SPLITS`). Mendeley 는 Roboflow 원본(분할 무관)과 닮은 사진을 통째로 빼고, Mendeley 안에서도 test → val → train 순서를 지킵니다. 하단 각도 분할은 연속 프레임이 학습과 검증에 섞이지 않도록 구간 단위로 나눴습니다.

빌드 도구는 출처·규칙·건수를 `datasets/ppe/build/<version>/data_card.json` 에 쓰지만, 그 폴더는 저장소에 없습니다. 이 작업 트리의 `datasets/` 에는 `README.md` 하나만 있어 실제 파일 수와 라벨 건수를 셀 수 없었습니다. 위 표의 «확인 못 함» 은 그 때문입니다. Release `ppe-v5` 의 `ppe.json` 에 데이터 카드 해시가 있다고 NOTICE 가 적고 있으나 이 문서를 쓸 때 열어 보지 않았습니다.

## 5. 학습 설정

| 항목 | v4 | v5 미세조정 | 출처 |
| :--- | :--- | :--- | :--- |
| 출발점 | COCO 사전학습 `yolox_s.pth` | v4 체크포인트 | [`config.yaml`](../config/config.yaml), 노트북 |
| epoch | 100 (마지막 20 은 증강 끔) | 30 (마지막 10 은 증강 끔) | [`yolox_exp_ppe_s.py`](../tools/ppe/yolox_exp_ppe_s.py), 노트북 |
| 학습률 · warmup | YOLOX 기본값(재정의 없음) · warmup 5 epoch | `0.01 / 64 × 0.3` per image · warmup 1 epoch | [`yolox_exp_ppe_s.py`](../tools/ppe/yolox_exp_ppe_s.py), 노트북 |
| batch · 정밀도 | 16 · fp16 | 16 · fp16 | 같음 |
| seed | 20260928 | 20261001 | 같음 |
| HSV 증강 게인 | (3, 15, 30). 주황·형광이 판정 단서라 약하게 둡니다 | v4 와 같음(노트북 `HSV_GAINS = (3, 15, 30)`) | [`yolox_exp_ppe_s.py`](../tools/ppe/yolox_exp_ppe_s.py) |
| mosaic | 0.5, scale (0.5, 1.5) | 같음 | 같음 |
| 환경 | 확인 못 함 | Colab T4, YOLOX 0.3.0 | 노트북 |

노트북은 출력이 지워진 채 커밋돼 있어, 학습 로그는 저장소에서 확인할 수 없습니다.

## 6. 운용 설정

| 항목 | 값 | 출처 |
| :--- | :--- | :--- |
| `conf_threshold` | 0.5 | [`config.yaml`](../config/config.yaml) `vision.ppe` |
| `crop_pad` | 0.08 | 같음 |
| 위반 확정 창 | 1500ms 안 3회 | 같음 |
| 머리 클리핑 여유 | 8px | 같음 |

## 7. 평가

### 7.1 공개 데이터 (후보 선택용)

| 지표 | v4 | v5 | 출처 |
| :--- | ---: | ---: | :--- |
| Roboflow test AP50 | 0.853 | 0.818 | PR #385, [`config.yaml`](../config/config.yaml) |
| 하단 각도 val AP50 | 0.969 | 1.0 | PR #385 |
| 하단 각도 착용자 58명 → 적합 / 오경보 / 판정 불가 | 43 / 2 / 13 | 58 / 0 / 0 | PR #385 |

설정 주석은 v4 이전 후보 v2 의 수치(Roboflow test AP50 0.858, Mendeley test AP50 0.966)도 남기고 있습니다. v5 의 Mendeley test AP50 은 확인 못 함입니다.

### 7.2 XIAO 실측 (채택 기준)

공개 데이터 점수는 후보 선택에만 쓰고, 채택은 XIAO 실측으로 판단합니다([PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md)). 2026-10-06 mechdog-01(카메라 높이 약 15cm, VGA)에서 직립 4구간을 찍어 C1~C3 를 모두 통과했습니다. 8499프레임 · 405.5초에서 적합 2113 · 위반 3380 · 확인불가 2544건이었고, 판정 가능률은 4880/5913 = 82.5% 였습니다([XIAO 실측](../field_tests/results/20261006_ppe-xiao/summary.md)).

**같은 XIAO 프레임에서 v4 와 비교.** 2026-10-06 밤 후면 재측정의 원본 프레임 5841장에 v4 와 v5 를 같은 파이프라인(사람 검출·크롭·판정 규칙·운용 창 동일, `ppe.onnx` 만 교체)으로 다시 돌렸습니다. v5 재추론은 구간 안 프레임 98.5% 에서 실기 기록과 같은 판정을 냈습니다. 후면 위반 2구간(머리 클리핑 제외)에서 확인불가는 v4 187건 → v5 113건, 실효 성공률은 92.8% → 95.6% 였고, 적합으로 오판한 판정은 v4 0건 · v5 2건이었습니다. 기대가 모두 위반인 구간이라 착용자 오경보는 비교하지 못했습니다([v4 대 v5 비교](../field_tests/results/20261006_ppe-xiao-rear/v4-vs-v5.md)).

**XIAO 평가 프레임과 학습 데이터의 관계.**

- 2026-10-06 실측 프레임은 학습에 쓰지 않았습니다. 프레임은 커밋하지 않습니다(`.gitignore`).
- 하단 각도 학습 데이터(3번)는 폰으로 찍은 사진을 XIAO 해상도로 줄인 것이고, XIAO 로 찍은 것이 아닙니다.
- XIAO 수집 규칙은 학습·검증 세션(a)과, (a)와 다른 날 또는 다른 장소(가능하면 다른 인물)에서 찍는 독립 시험 세션(b)을 나눕니다. 2026-09-28 세션은 (a) 에만 씁니다. 수집 기록 표는 아직 비어 있어, XIAO 프레임이 v5 학습에 들어갔는지는 기록상 «들어가지 않았다» 로 읽힙니다 [추측: NOTICE 의 학습 데이터 목록에 XIAO 프레임이 없음].
- 하단 각도 촬영 인물과 2026-10-06 실측 피험자가 같은 사람인지는 확인 못 함입니다.

## 8. 알려진 한계

1. **뒤에서 본 맨머리 판정이 촬영 조건에 따라 크게 다릅니다.** 2026-10-06 낮 `standing-nohelmet` 후면은 314건 중 235건을 적합으로 오판했지만, 같은 날 밤 후면만 다시 잰 측정에서는 1285건 중 오판이 2건이었습니다(1257건 정답, 97.8%). 모델과 판정 경로가 같아 차이는 촬영 조건에서 왔으나, 낮 프레임이 덧그린 이미지라 원인은 가리지 못했습니다([후면 재측정](../field_tests/results/20261006_ppe-xiao-rear/analysis.md)). C1~C3 에는 잡히지 않습니다.
2. **합격 범위가 직립 4구간뿐입니다.** 웅크림·머리 잘림·자세 상승 구간은 XIAO 로 재지 않았습니다.
3. **하단 각도 데이터는 한 사람·한 장소입니다.** 착용 상태만 찍어 하단 각도의 위반(맨머리·조끼 없음) 예시는 없습니다 [추측: NOTICE 가 «안전모·조끼를 착용한 팀원» 만 적음].
4. **머리 박스 규약이 데이터마다 다릅니다.** Mendeley 는 얼굴을 포함한 세로형, Roboflow 는 가로형입니다([PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md)).
5. **Mendeley 사람 박스는 COCO 검출기 산출물입니다.** 검출기가 놓친 사람은 학습에서도 빠집니다.
6. **부분 라벨 제외의 효과는 통제 실험으로 재지 않았습니다**(사례 6).
7. **공개 test 점수는 v5 에서 내려갔습니다**(0.853 → 0.818). 로봇 시점을 위해 정면 공개 사진 성능을 일부 내준 결과로 기록돼 있습니다.

## 9. 다른 문서와의 관계

- [datasets/README.md](../datasets/README.md) 의 `ppe/` 절은 저장소 밖 데이터의 폴더 규약을 적습니다. 실제 분할 구성은 이 카드를 따릅니다.
- [models/ppe-v12-candidate.md](../models/ppe-v12-candidate.md) 는 개발 후보 v12 의 기록이며, 그 안의 «기본 운영 모델은 ppe-v4» 문구는 v5 배포 전의 것입니다.
