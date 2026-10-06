# 사례 6. 부분 라벨이 맨머리를 «배경» 으로 가르친 사례

- 관련 문서: [PPE 학습 기록 1.6 · 3.4](../internal/PPE_TRAIN.md), [PPE 데이터 카드](../ppe-data-card.md)
- 측정 기록: [3.7.2 공개 데이터 평가](../../field_tests/results/20260918_3.7.2-ppe-model/report.md)
- 도구와 테스트: [`rf100_prepare.py`](../../tools/ppe/rf100_prepare.py), [`augment_torso.py`](../../tools/ppe/augment_torso.py), [`test_ppe_retrain_tools.py`](../../tests/test_ppe_retrain_tools.py)

## 초기 가설

공개 PPE 데이터셋 4개를 병합하면(3,219장, 박스 12,064개) 클래스마다 박스가 충분하니 데이터 양으로 성능이 나온다고 봤습니다. 라벨이 한 사진 안에서 빠짐없이 달려 있다는 점은 확인하지 않은 전제였습니다([PPE_TRAIN.md 1.1~1.4](../internal/PPE_TRAIN.md)).

## 관측된 실패

첫 모델(YOLOX-Nano)의 공개 검증 결과에서 위반 클래스가 가장 약했습니다([3.7.2 보고서](../../field_tests/results/20260918_3.7.2-ppe-model/report.md)).

| 클래스 | 박스 recall | AP |
| :--- | ---: | ---: |
| helmet | 0.878 | 49.9 |
| vest | 0.796 | 54.5 |
| no_vest | 0.762 | 35.3 |
| no_helmet | 0.627 | 30.5 |

사진 단위 오알람의 원인 클래스는 no_vest 145건, no_helmet 69건이었습니다([PPE_TRAIN.md 3.3~3.6](../internal/PPE_TRAIN.md)).

## 근거 데이터

- **544장에 머리 라벨이 없었습니다.** 3,219장 중 544장은 몸통만 라벨되어 있고 머리 라벨이 없습니다. 학습 문서는 «머리가 보이는데 라벨이 없으면 그 영역을 배경으로 학습한다» 고 적고, 이를 no_helmet 검출력이 낮은 원인으로 유력하게 봤습니다([PPE_TRAIN.md 1.6](../internal/PPE_TRAIN.md)).
- **클래스 비중이 기울어 있었습니다.** no_vest 박스(4,227)가 vest 박스(2,133)의 두 배여서 조끼 오알람의 원인으로 의심했습니다. 안전모만 빠진 사진은 252장(7.8%)뿐이었습니다([PPE_TRAIN.md 1.4 · 3.4](../internal/PPE_TRAIN.md)).
- **다음 데이터셋에서도 같은 형태가 나왔습니다.** Mendeley PPE2286 은 사람 라벨이 없고, 보강용 데이터(ds2)에는 조끼 라벨이 없었습니다. 하단 각도 자동 라벨 400프레임 중 121프레임은 라벨이 불완전해 뺐습니다([models/NOTICE](../../models/NOTICE)).

## 원인

검출기는 라벨이 없는 영역을 «아무것도 없음» 으로 배웁니다. 사람의 머리가 보이는데 `helmet`·`no_helmet` 어느 쪽 박스도 없으면, 모델은 그 머리를 배경으로 학습하고 맨머리 검출 점수를 스스로 낮춥니다. 데이터의 빈칸이 모델의 미검출로 바뀌는 경로입니다. 다만 544장을 뺀 학습과 넣은 학습을 같은 조건으로 비교한 적은 없어서, 이 경로가 no_helmet 약세의 몇 %를 설명하는지는 [추측]입니다.

## 변경

1. **사람 단위 라벨 검사를 만들었습니다.** `person_label_problem(person, ppe)` 는 사람 박스 안에 머리 라벨이 없으면 `REASON_NO_HEAD`, 몸통 라벨이 없으면 `REASON_NO_TORSO` 를 돌려주고, 그 사람은 학습에서 뺍니다. 도구 설명에 «실제 맨머리나 몸통이 배경으로 학습된다 (PPE_TRAIN.md 1.6 결함과 같은 형태)» 라고 이유를 적었습니다([`rf100_prepare.py`](../../tools/ppe/rf100_prepare.py)). 테스트는 `test_person_label_check_requires_head_and_torso` 입니다([`test_ppe_retrain_tools.py:83`](../../tests/test_ppe_retrain_tools.py)). 모르는 클래스 이름이 나오면 변환을 멈춥니다.
2. **몸통 라벨을 확신할 때만 채웠습니다.** ds2 는 조끼 라벨이 없어, 확신이 높은 몸통만(VEST_MIN 0.30) 채우고 애매한 사진은 통째로 뺐습니다([`augment_torso.py`](../../tools/ppe/augment_torso.py)).
3. **중복과 분할 누수를 막았습니다.** Mendeley 사진 중 Roboflow 원본과 같은 사진(좌우 반전 포함)은 dHash 로 뺐습니다(PR #337, [`mendeley_prepare.py`](../../tools/ppe/mendeley_prepare.py)). Roboflow 변환은 test → val → train 순서로 처리해 겹치면 평가 쪽이 사진을 가져갑니다([`rf100_prepare.py`](../../tools/ppe/rf100_prepare.py)).
4. **자동 라벨도 같은 규칙을 따랐습니다.** 하단 각도 Colab 노트북은 «반쪽 라벨은 «안 보이는 것» 으로 배운다» 는 이유로 불완전한 자동 라벨 프레임을 학습에서 뺍니다([`colab_ppe_v5_lowangle.ipynb`](../../tools/ppe/colab_ppe_v5_lowangle.ipynb)).

## 동일 조건 재평가

**같은 조건 재평가는 없습니다.** 부분 라벨을 뺀 뒤의 모델은 구조(Nano → S)와 데이터셋 구성이 함께 바뀌어, 3.7.2 의 no_helmet recall 0.627 과 직접 비교할 수 없습니다. 참고로 같은 잣대로 비교한 후보 중 Roboflow no_helmet 재현율은 v1 71.4%, v2 85.7% 였습니다([PPE_ACCEPTANCE.md](../PPE_ACCEPTANCE.md)). v2 는 Mendeley 를 더하고 학습 no_helmet 박스가 80개에서 346개로 늘어난 모델이라, 이 상승을 라벨 검사의 효과로 볼 수는 없습니다.

## 남은 한계

- 학습 문서의 후속 계획은 «머리 라벨이 없는 544장을 제거한 데이터셋으로 재학습한다» 와 «재학습 후 같은 검증셋으로 다시 재서 개선 여부를 비교한다» 였습니다([PPE_TRAIN.md 6.8](../internal/PPE_TRAIN.md)). 그 비교 결과는 저장소에서 찾지 못했습니다. 같은 모델·같은 시드로 544장만 바꾼 통제 실험을 하면 효과를 분리할 수 있습니다.
- Mendeley 와 Roboflow 는 머리 박스 규약이 다릅니다(Mendeley 는 얼굴을 포함한 세로형, Roboflow 는 가로형). 이 차이가 모델에 주는 영향은 재지 않았습니다.
- 사람 라벨이 없는 Mendeley 는 런타임 COCO 검출기로 사람 박스를 만들었으므로, 검출기가 놓친 사람은 라벨 검사 대상에서도 빠집니다.
