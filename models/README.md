# 모델 가중치

용량이 커서 Git에 커밋하지 않는다 (`.gitignore` 참조).

검출기는 **2단 구조**다 (PRD DR-12). ①번 범용 검출기가 `person` 을 찾은 프레임에서만
②번 PPE 모델이 돌아간다. 사람이 없는 프레임에서 PPE 추론을 돌리지 않기 위한 게이팅이다.

## 필요한 파일

| 파일 | 용도 | 확보 방법 | 상태 |
| :--- | :--- | :--- | :--- |
| `coco.onnx` | ① 범용 검출기 — 사람 검출(FR-3) + 변화 감지 대상 객체(FR-8) | **YOLOX-S 공식 배포 ONNX 를 받아 이름만 바꾼다.** 학습·export 불요 | ✅ 계열 확정 (ADR-24) |
| `ppe.onnx` | ② PPE 전용 — `helmet` / `no_helmet` / `vest` / `no_vest` (FR-9) | **새 데이터셋으로 직접 학습**한다. `person` 은 ①이 담당하므로 학습 대상에서 제외 | ⬜ 데이터셋 미정 |

---

## ① `coco.onnx` 획득 절차 — YOLOX-S

**모델 계열은 `YOLOX` 로 확정했다 (2026-09-10 · OI-15 닫힘 · [ADR-24](../docs/DECISIONS.md)).**

| | |
| :--- | :--- |
| 출처 | `Megvii-BaseDetection/YOLOX` 릴리스 **`0.1.1rc0`** 의 자산 `yolox_s.onnx` |
| URL | `https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx` |
| **라이선스** | **Apache-2.0** · Copyright (c) 2021-2022 Megvii Inc. — 저장소 `LICENSE` 원본으로 확인 |
| 크기 | **35,858,002 바이트** (약 34.2 MiB) |
| 입력 | `1 x 3 x 640 x 640` (float32, **0~255 그대로**) |
| 출력 | `1 x 8400 x 85` — 중심 오프셋 2 · 크기 로그 2 · objectness 1 · 클래스 80 |

```bash
curl -L -o models/coco.onnx https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx
```

**받은 뒤 반드시 크기와 해시를 확인한다.**

```bash
sha256sum models/coco.onnx && stat -c %s models/coco.onnx
```

| | 값 |
| :--- | :--- |
| 기대 크기 | `35858002` |
| SHA-256 | ⬜ **첫 다운로드 시 여기에 적는다** |

> ⚠️ **릴리스 자산에 게시된 다이제스트가 없다.** 그래서 처음 받은 사람이 해시를
> 계산해 위 표에 적고, 이후에는 그 값과 대조한다. **적어 두지 않으면 몇 달 뒤에
> "같은 파일인가"를 확인할 방법이 없다** — 펌웨어 백업을 SHA-256 으로 보존한 것과
> 같은 이유다.

> ⚠️ **가중치 파일은 저장소에 넣지 않는다.** 없으면 `Detector.open()` 이
> `ModelMissingError` 로 즉시 멈추고 이 문서를 가리킨다. 벤더 라이브러리를
> `#error` 가드로 처리한 것([ADR-20](../docs/DECISIONS.md))과 같은 원칙이다.

### 클래스 어휘

COCO 80클래스이며 **순서가 모델과의 계약**이다. 목록은 `host/vision/coco_labels.py`
에 있고 `config.yaml` 에 두지 않았다 — 튜닝할 값이 아니고, 사람이 고칠 수 있게 두면
언젠가 순서가 흐트러진다. **순서가 어긋나면 사람을 자전거로 부르면서도 오류는 나지
않는다.**

FR-8 변화 감지는 **개방 어휘를 쓰지 않는다.** 시연에 놓을 소품을 COCO 80 안에서
고르는 쪽이 싸고 확실하다(`vision.coco.change_watch_classes`). 시험이 그 값들이
어휘 안에 있는지 검사한다.

## 배치

```
models/
├── coco.onnx
└── ppe.onnx
```

경로는 `config/config.yaml` 의 `vision.coco.model_path` / `vision.ppe.model_path` 에서 관리한다.

## 확정 시 반드시 기록할 것

모델 계열과 학습 프레임워크 버전을 `config.yaml` 의 `vision.coco.model_family` /
`vision.ppe.model_family` 에 적는다. `coco` 는 **`yolox` 로 확정**했고 `ppe` 는 아직
`null` 이다.

`model_family` 는 장식이 아니라 **동작을 고르는 값**이다 —
`host/vision/detector.py` 의 `ADAPTERS` 에서 전처리·디코딩 규약을 결정하며,
모르는 이름을 적으면 기동이 거부된다.

가중치 파일이 저장소에 없으므로, **이 값이 비어 있으면 몇 달 뒤 같은 결과를 재현할 수 없다** (FR-9.1.2).
