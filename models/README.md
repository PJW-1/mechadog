# 모델 가중치

용량이 커서 Git에 커밋하지 않는다 (`.gitignore` 참조).

**별도 개발 후보:** [PPE v12 5종 공동학습 모델](ppe-v12-candidate.md)은
`python tools/fetch_models.py --ppe-candidate`로 받을 수 있다.
기본 모델·운영 설정은 유지하며, 실기 미승인 상태와 비교 결과는 링크에서 확인한다.

검출기는 **2단 구조**다 (PRD DR-12). ①번 범용 검출기가 `person` 을 찾은 프레임에서만
②번 PPE 모델이 돌아간다. 사람이 없는 프레임에서 PPE 추론을 돌리지 않기 위한 게이팅이다.

## 필요한 파일

| 파일 | 용도 | 확보 방법 | 상태 |
| :--- | :--- | :--- | :--- |
| `coco.onnx` | ① 범용 검출기 — 사람 검출(FR-3) + 변화 감지 대상 객체(FR-8) | **YOLOX-S 공식 배포 ONNX 를 받아 이름만 바꾼다.** 학습·export 불요 | ✅ 계열 확정 (ADR-24) |
| `ppe.onnx` | ② PPE 전용 — `helmet` / `no_helmet` / `vest` / `no_vest` / `person_down` (FR-9) | **`python tools/fetch_models.py`** — 팀 Release 자산(`ppe-v3`)에서 받고 크기·SHA-256을 자동 검증한다. `person`은 ①이 담당한다 | ✅ 학습·ONNX export 완료, XIAO 실기 검수 대기 |

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

### 받는 방법 — **명령 하나다**

```bash
python tools/fetch_models.py
```

없는 것만 받고 **크기와 SHA-256 을 자동으로 검증**한다. 이미 있으면 검증만 한다.

| | |
| :--- | :--- |
| `--check` | 받지 않고 현재 파일만 검증 |
| `--force` | 있어도 다시 받는다 |

> ⚠️ **`curl` 을 손으로 쓰지 않는다.** 그렇게 적어 두면 사람은 다운로드 줄만
> 복사하고 **해시 확인 줄은 건너뛴다.** 그래서 검증을 선택할 수 없게 스크립트
> 한 곳에 묶었다 — *복원해 본 적 없는 백업은 백업이 아니다* 와 같은 이유다.

| 기대값 | |
| :--- | :--- |
| 크기 | **35,858,002** 바이트 |
| SHA-256 | `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` |

> ⚠️ **정본은 `tools/fetch_models.py` 의 `WEIGHTS` 다.** 위 표는 사람이 읽게 옮긴
> 것이며, 둘이 어긋나지 않도록 **시험이 대조한다**.
>
> ⚠️ **릴리스 자산에 게시된 다이제스트가 없다.** 그래서 2026-09-10 에 처음 받은
> 값을 기준으로 박아 두었다. **크기만 보면 안 된다** — 잘린 다운로드는 크기로
> 잡히지만 바뀐 파일은 크기가 같을 수 있다.

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

## ③ 로컬 VLM — `Qwen2-VL-2B-Instruct`

**확정 (2026-09-17 · [ADR-35](../docs/DECISIONS.md#adr-35)).** 객체 목록으로는 *"쓰러져
있다"* 를 말할 수 없어 사진을 그대로 읽는 모델을 하나 둔다. 쓰는 곳은
`host/vision/vlm_reader.py`(WBS `4.8.0`)다.

| | |
| :--- | :--- |
| 출처 | Hugging Face `Qwen/Qwen2-VL-2B-Instruct` |
| 정밀도 | **bf16** — 2단 샤드 `model-0000{1,2}-of-00002.safetensors` (3,988,609,112 + 429,441,656 바이트) |
| 실측 | VRAM **4.11 GB** · 적재 **14.5초** · 질문당 **0.2초** 안팎 — RTX 3080 · 2026-09-20 ([ADR-35](../docs/DECISIONS.md#adr-35) 는 적재를 5.5초로 적었으나 재측정 3회가 모두 14.5초였다) |
| 적재 시점 | **`factory` 모드에서만** ([ADR-35](../docs/DECISIONS.md#adr-35)). 개정 전에는 *"`assist` 의 음성 LLM 4.9GB 와 동시에 못 올린다"* 를 이유로 적었으나, 음성 LLM 과 `assist` 모드는 2026-09-23 폐기했다([ADR-38](../docs/DECISIONS.md#adr-38)) |

### ⚠️ 이 모델만 규칙이 셋 다르다

**ⓐ `models/` 에 두지 않는다.** `transformers` 가 Hugging Face 캐시
(`~/.cache/huggingface/hub/`)에서 직접 읽는다. 샤드·인덱스·토크나이저가 한 벌로
움직이는 구조라 파일 하나를 떼어 `models/` 에 복사하면 오히려 깨진다. 저장소에
넣지 않는 원칙([ADR-20](../docs/DECISIONS.md))은 그대로다.

**ⓑ `tools/fetch_models.py` 가 받지 않는다.** 저 스크립트는 URL 하나 + SHA-256 하나인
단일 파일용이다. 여기는 파일이 여러 개이고 Hugging Face 가 자체 해시로 검증하므로
같은 틀에 넣으면 검증을 두 겹으로 흉내 내는 꼴이 된다.

```bash
huggingface-cli download Qwen/Qwen2-VL-2B-Instruct
```

**ⓒ 없어도 멈추지 않는다.** `coco.onnx` 가 없으면 `Detector.open()` 이
`ModelMissingError` 로 즉시 서지만, VLM 은 **Tier 3** 이라 기록만 남기고 지나간다
(ADR-35 결정 6 · `VlmReader.load()` 가 거짓을 돌려준다). 사람 인지와 주행은 VLM 없이
그대로 돌아야 한다 — 판독기가 로봇을 세우면 Tier 구분이 무의미해진다.

### 환경 세팅 — 처음 하는 사람을 위한 절차

⚠️ **VLM 을 안 쓸 거면 아무것도 안 해도 된다.** 판독기는 가중치와 라이브러리가 없으면
«없음» 으로 동작하고 변화 감지·순찰·추종은 그대로 돈다(Tier 3 · ADR-35 결정 6). 시험도
전부 통과한다 — `tests/test_vlm_reader.py` 가 모델 없이 닫히도록 짜여 있다. **아래는
실제로 판독을 돌려 볼 사람만** 하면 된다.

#### ⓪ 왜 `requirements.txt` 에 없나

`transformers` 와 CUDA 빌드 `torch` 를 합치면 **2.7GB** 다. 공통 requirements 에 넣으면
VLM 을 쓰지 않는 CI 와 다른 팀원 PC 까지 그 비용을 치른다. 그래서 **필요한 사람이 자기
환경에 따로 깐다** — 가중치를 저장소에 넣지 않는 것과 같은 이유다.

#### ① 격리 환경을 만든다 — **시스템 파이썬에 깔지 마라**

⚠️ **기존 `torch` 를 덮어쓰면 다른 것이 깨진다.** 2026-09-20 에 확인해 보니 개발 PC 의
시스템 파이썬에서 `torch` 를 `isaacsim-core`·`torchvision`·`torchaudio`·`easyocr`·
`ultralytics` 가 물고 있었다. CUDA 빌드로 갈아끼우면 이것들이 같이 깨진다.

```bash
# uv 가 있으면 (빠르다)
uv venv ~/.venv-mechdog-vlm --python 3.12

# 없으면
python -m venv ~/.venv-mechdog-vlm
```

환경은 **홈 디렉터리에 둔다.** 저장소 안에 만들지 않는다 — 펌웨어 빌드 환경을
`~/.arduino-mechdog` 에 두는 것과 같은 관례다.

#### ② torch 를 **CUDA 빌드로** 넣는다

```bash
uv pip install --python ~/.venv-mechdog-vlm/Scripts/python.exe \
    torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

⚠️ **`torchvision` 을 빠뜨리지 마라.** 없으면 모델이 아니라 **프로세서**를 만들 때
터진다 — `Qwen2VLVideoProcessor requires the Torchvision library`. 영상 처리를 안 쓰는데도
`AutoProcessor` 가 딸린 비디오 프로세서를 같이 만들기 때문이다.

⚠️ **`cu128` 은 RTX 3080 기준이다.** 다른 GPU 면 <https://pytorch.org/get-started/locally/>
에서 맞는 인덱스를 고른다. 기본 인덱스(`pip install torch`)는 **CPU 빌드**가 깔려서
`torch.cuda.is_available()` 이 거짓이 된다.

#### ③ 나머지를 넣는다

```bash
uv pip install --python ~/.venv-mechdog-vlm/Scripts/python.exe \
    transformers accelerate qwen-vl-utils pillow
```

#### ④ 가중치를 받는다

```bash
huggingface-cli download Qwen/Qwen2-VL-2B-Instruct
```

`~/.cache/huggingface/hub/` 에 약 **4.2GB** 로 들어간다. `models/` 로 복사하지 않는다(위 ⓐ).

#### ⑤ 확인

```bash
~/.venv-mechdog-vlm/Scripts/python.exe -c "import torch, transformers; \
print(torch.__version__, torch.cuda.is_available(), transformers.__version__)"
```

`2.11.0+cu128 True 5.17.0` 처럼 **가운데가 `True`** 면 됐다. `False` 면 ②를 다시 본다 —
CPU 빌드가 깔린 것이고, 그 상태로 돌리면 질문 하나에 수십 초가 걸린다.

실제로 돌려 보려면 재현 스크립트가 있다:

```bash
~/.venv-mechdog-vlm/Scripts/python.exe \
    TEST_MECHDOG/results/20260920_4.8.0-vlm-compare/vlm_compare_bench.py out.json
```

`blackbox/` 에 쌓인 실기 프레임을 재료로 쓴다. 정상이면 적재 후 VRAM **4.1GB**, 질문당
**0.2초** 안팎이 나온다. 이 수치가 크게 다르면 CPU 로 돌고 있거나 다른 정밀도로 올라간
것이다.

### ⚠️ 4bit 로 줄이지 않는다

VRAM 을 2.3GB 까지 줄일 수 있지만 실측에서 판독이 무너졌다(ADR-35 대안 ⓐ). 애초에
양자화를 본 것은 모드가 둘이던 시절 한꺼번에 올리려다 10GB 를 넘긴 탓이고, 모드를
셋으로 쪼개 `factory` 가 ~7.1GB 로 들어가는 지금은 정밀도를 깎을 이유가 없다
([MODEL_PLAN 0절](../docs/MODEL_PLAN.md)).

## 배치

```
models/
├── coco.onnx
└── ppe.onnx        # VLM 은 여기 없다 — ③ 참조 (HF 캐시)
```

`ppe.onnx`의 정본 식별값은 크기 **3,677,797바이트**, SHA-256
`a294c1b7d887fe9a80888adf5335602c741727c4963578beea183fc2876e8ed4`이다. 자체 학습
산출물이라 공개 URL이 없어 **팀 Release 자산**(태그 `ppe-v3`)으로 배포하며, `coco.onnx`와 같이
`tools/fetch_models.py`가 받고 두 값을 검증한다. 학습·export 재현 절차는
`firmware_xiao_vision/PPE_Train.md`에 있다.

**라이선스 — `models/NOTICE` 를 함께 배포한다.** v3 가중치는 라이선스가 혼합된 데이터셋(NOTICE §3)에서
나온 파생물이라 배포 조건이 **검토 중**이다 — 현재 릴리스는
팀 내부 테스트용이고, 베이스 모델 YOLOX는 Apache-2.0이다. 저장소 코드 자체는 루트 `LICENSE`(Apache-2.0)를 따른다. **가중치만
떼어 전달하면 출처 표시가 끊기므로 `NOTICE`를 같이 넣는다.**

경로는 `config/config.yaml` 의 `vision.coco.model_path` / `vision.ppe.model_path` 에서 관리한다.

## 확정 시 반드시 기록할 것

모델 계열과 학습 프레임워크 버전을 `config.yaml` 의 `vision.coco.model_family` /
`vision.ppe.model_family` 에 적는다. `coco` 와 `ppe` 모두 **`yolox` 로 확정**됐다.

`model_family` 는 장식이 아니라 **동작을 고르는 값**이다 —
`host/vision/detector.py` 의 `ADAPTERS` 에서 전처리·디코딩 규약을 결정하며,
모르는 이름을 적으면 기동이 거부된다.

가중치 파일이 저장소에 없으므로, **이 값이 비어 있으면 몇 달 뒤 같은 결과를 재현할 수 없다** (FR-9.1.2).
