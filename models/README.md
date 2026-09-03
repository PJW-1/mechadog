# 모델 가중치

용량이 커서 Git에 커밋하지 않는다 (`.gitignore` 참조).

검출기는 **2단 구조**다 (PRD DR-12). ①번 범용 검출기가 `person` 을 찾은 프레임에서만
②번 PPE 모델이 돌아간다. 사람이 없는 프레임에서 PPE 추론을 돌리지 않기 위한 게이팅이다.

## 필요한 파일

| 파일 | 용도 | 확보 방법 | 상태 |
| :--- | :--- | :--- | :--- |
| `coco.onnx` | ① 범용 검출기 — 사람 검출(FR-3) + 변화 감지 대상 객체(FR-8) | COCO 사전학습 가중치를 ONNX 로 내보낸다. **학습 불요** | ⬜ 모델 계열 미정 |
| `ppe.onnx` | ② PPE 전용 — `helmet` / `no_helmet` / `vest` / `no_vest` (FR-9) | **새 데이터셋으로 직접 학습**한다. `person` 은 ①이 담당하므로 학습 대상에서 제외 | ⬜ 데이터셋 미정 |

> **모델 계열은 아직 확정하지 않았다 (OI-15).** YOLO 계열로 못박지 않는다.
> 선정 기준은 ⓐ FR-8 변화 감지 어휘(COCO 고정 vs 개방 어휘) ⓑ 기준 PC에서 프레임 예산 내 동작
> ⓒ **라이선스** (`ultralytics` = AGPL-3.0 / RT-DETR·MMDetection·YOLOX = Apache-2.0) 이며, M2 에서 결정한다.

## 배치

```
models/
├── coco.onnx
└── ppe.onnx
```

경로는 `config/config.yaml` 의 `vision.coco.model_path` / `vision.ppe.model_path` 에서 관리한다.

## 확정 시 반드시 기록할 것

모델 계열과 학습 프레임워크 버전을 `config.yaml` 의 `vision.coco.model_family` /
`vision.ppe.model_family` 에 적는다. 지금은 둘 다 `null` 이다.

가중치 파일이 저장소에 없으므로, **이 값이 비어 있으면 몇 달 뒤 같은 결과를 재현할 수 없다** (FR-9.1.2).
