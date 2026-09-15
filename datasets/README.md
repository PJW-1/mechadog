# 데이터셋

용량과 **라이선스** 때문에 Git에 커밋하지 않는다 (`.gitignore` 참조).

## `coco_samples/` — 검출 정확도 확인용 사진 3장

```bash
python tools/fetch_models.py --samples
```

### 왜 필요한가 — **잡음으로는 정확도를 확인할 수 없다**

`3.3.1` 의 속도 실측(8.2ms)은 잡음 프레임으로 했다. CNN 의 연산량은 입력 내용과
무관하므로 **속도로는 유효하지만 정확도로는 아무것도 말하지 않는다.**

실제로 확인했다 — `/255` 정규화를 넣어 **일부러 고장낸** 전처리도 잡음에서 정상과
**똑같이 0건**을 냈다. **잡음 시험은 정상과 완전 고장을 구분하지 못한다.**

| 파일 | 무엇을 확인하나 |
| :--- | :--- |
| `000000001000.jpg` | **사람 7명 + 테니스 라켓** — 간판 기능(`person`) |
| `000000004495.jpg` | **500x375 → ratio 1.28** — letterbox 되돌리기. 대부분의 COCO 사진은 비율이 1.0 이라 이 경로가 검증되지 않는다 |
| `000000000139.jpg` | 실내 — `person`·`chair`·`potted plant`. FR-8 변화감지 어휘 |

### ⚠️ 왜 커밋하지 않는가

**COCO 주석은 CC-BY-4.0 이지만 사진 자체는 Flickr 개별 라이선스**다. 우리가 재배포할
근거가 없다. 벤더 라이브러리를 저장소에 넣지 않은 것([ADR-20](../docs/DECISIONS.md))과
같은 판단이며, 그래서 **URL·크기·SHA-256 만 기록**하고 파일은 각자 받는다.

정본은 `tools/fetch_models.py` 의 `SAMPLES` 다. 위 표는 사람이 읽게 옮긴 것이며,
어긋나지 않도록 시험이 대조한다.

### 이것으로 검증한 것 (2026-09-10)

박스를 그려 **눈으로 대조**했다. 숫자만 보면 "무언가 나왔다" 까지만 알 수 있고,
박스가 그 물체에 얹혔는지는 봐야 안다.

| 사진 | 결과 |
| :--- | :--- |
| 사람 7명 | `person` 7건 (0.83~0.92) + `tennis racket` 0.82 — **박스가 사람마다 정확히 얹혔다** |
| ratio 1.28 | `chair` 0.92 · `tv` 0.92 · `couch` 0.64 — **좌표 복원 정상** |
| 실내 | `chair` 0.82 · `person` 0.78 · `refrigerator` 0.66 · `potted plant` 0.64 |

⚠️ **이것은 "동작한다" 는 확인이고 성능 평가가 아니다.** mAP 를 재지 않았고 3장은
표본이 아니다. **실기(XIAO) 프레임 검증은 여전히 남아 있다** — 조명·화질·화각이 다르다.

## `ppe/` — PPE 학습 데이터셋

`person` 은 ①번 검출기가 담당하므로 **학습 대상에서 제외**한다 (ADR-12).
클래스 순서는 `config.yaml` 의 `vision.ppe.classes` 와 같다:

    0 helmet · 1 no_helmet · 2 vest · 3 no_vest

### 구조

```
ppe/
  images/{train,val,test}/<session>/*.jpg
  labels/{train,val,test}/<session>/*.txt   # YOLO 정규화: class cx cy w h
  preview/<session>/*.jpg                   # 박스를 그린 검수용 (학습 아님)
```

- **`<session>` = 촬영 장소_날짜** (예: `lab_20260915`, `sim_factory`). 분할은
  파일이 아니라 **세션 단위**로 한다 — 같은 장소의 연속 프레임이 train/test 에
  섞이면 "새 상황을 아는가" 가 아니라 "같은 사진을 외웠는가" 를 재게 된다.
- **합성 데이터(`sim_*` 세션)는 `train` 에만 둔다.** val/test 는 실사여야
  "시뮬에서만 잘 맞는 모델" 을 조기에 걸러낼 수 있다.
- 각 세션은 `labels/<split>/<session>/_manifest.csv` 에 출처를 남긴다.

### 두 생산 경로

| 경로 | 도구 | 라벨 근거 |
| :--- | :--- | :--- |
| 실사 촬영 | `tools/ppe_seed_labels.py` | coco.onnx person 박스 + HSV 색 비율 **초안** — 애매한 것은 `uncertain` 으로 사람에게 넘긴다. **사람 검수 필수** |
| 시뮬 합성 | `sim/gen_ppe_dataset.py` | 스폰 정답 그 자체 — 부위 prim 투영 박스 + 깊이 가림 검사. 캐릭터 에셋은 복장 고정(매니페스트 `c:` 태그) |

```bash
# 실사: 촬영 프레임에 초안 라벨 → preview 보며 검수
python tools/ppe_seed_labels.py --images datasets/ppe/images/train \
    --labels datasets/ppe/labels/train --preview datasets/ppe/preview/train

# 합성: Isaac venv 로 생성
C:/Users/a9800/isaac_clean/venv/Scripts/python.exe \
    sim/gen_ppe_dataset.py --count 50 --preview datasets/ppe/preview/train
```

### 데이터 카드

세션마다 `datacard.yaml` 을 둔다 — 출처가 뭉개지면 나중에 "이 데이터가 왜
틀렸나" 를 추적할 수 없다.

```yaml
session: lab_20260915        # 장소_날짜
source: real | sim           # 실사 | 합성
camera: xiao-vga | isaac-fpv # 어느 시점에서 찍었나
split: train                 # 이 세션이 속한 분할
labels: seed+reviewed        # seed=초안 그대로, +reviewed=사람 검수 완료
notes: 조명·복장 등 특이사항
```

⚠️ **초안 라벨은 라벨이 아니다** — `seed` 만 찍힌 세션으로 학습하면 색 규칙의
틀린 제안까지 정답으로 배운다. `+reviewed` 전에는 학습에 섞지 않는다.
