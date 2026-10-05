# VLM LoRA — `Qwen2-VL-2B-Instruct` 미세조정 도구

운용 VLM 의 예/아니오 판독(특히 LiDAR 막힘 확정 프레임의 `blocked_by_fallen`)을 우리 사진으로
LoRA 미세조정한다. [ADR-43](../../docs/DECISIONS.md#adr-43) «재검토» 의 향후 방향을 따른다:
비전 인코더는 얼리고 언어 모델 쪽 LoRA, 학습 뒤 bf16 으로 병합, **다른 날·다른 배치**의
보류 세트로 따로 평가.

⚠️ **사진·목록·어댑터·병합 모델은 저장소 밖에 둔다.** 사진에는 얼굴이 찍힐 수 있다.

## 순서

| 단계 | 파일 | GPU |
| :--- | :--- | :--- |
| ① 목록 만들기·분할 | [dataset.py](dataset.py) | 불필요 |
| ② 학습 | [train.py](train.py) | 필요 |
| ③ 병합 | [merge.py](merge.py) | 불필요 (CPU 로 병합) |
| ④ 평가 | [evaluate.py](evaluate.py) | 필요 |

## 환경

VLM 전용 환경 `~/.venv-mechdog-vlm`([models/README.md](../../models/README.md) ③)에 `peft` 만 더한다.

```bash
uv pip install --python ~/.venv-mechdog-vlm/Scripts/python.exe peft
```

## 데이터 폴더

- 연출 촬영: `<root>/<질문키>/<yes|no>/<장면>_NN.jpg` — `tools/probe/vlm_bench.py capture` 로 찍는다.
  질문키는 운용 질문 셋 `vlm_reader.QUESTIONS` 의 키다. 질문 문장도 실행할 때 그곳에서 읽는다 —
  키가 없으면(예: `blocked_by_fallen` 이 아직 운용 질문에 없을 때) «질문 키 … 이 vlm_reader.QUESTIONS 에 없다» 로 멈춘다.
- 자동 수집: `<root>/<YYYYMMDD>/<blocked|clear>/<파일>.jpg` + 날짜 폴더의 `manifest.jsonl`.

자동 수집 라벨은 «막혔나» 만 안다. 그래서 `clear` 는 `blocked_path`·`blocked_by_fallen` 둘 다
«아니오», `blocked` 는 `blocked_path` «예» 이고 `blocked_by_fallen` 은 **미상**(`split: review`)으로
남아 학습·평가에서 빠진다. 미상 사진은 사람이 보고 연출 폴더 형식으로 옮겨 넣는다.

## 명령

```bash
PY=~/.venv-mechdog-vlm/Scripts/python.exe
python tools/vlm_lora/dataset.py --staged D:/vlm/staged --auto D:/vlm/auto --out D:/vlm/list.jsonl
$PY tools/vlm_lora/evaluate.py D:/vlm/list.jsonl --base-only            # 학습 전 기준선
$PY tools/vlm_lora/train.py D:/vlm/list.jsonl --out D:/vlm/adapter-01 --keys blocked_by_fallen blocked_path
$PY tools/vlm_lora/merge.py D:/vlm/adapter-01 --out D:/vlm/merged-01
$PY tools/vlm_lora/evaluate.py D:/vlm/list.jsonl --merged D:/vlm/merged-01 --out D:/vlm/eval-01.json
```

- 분할: 기본은 가장 늦은 날짜를 보류한다. `--holdout-date`·`--holdout-group` 으로 정한다.
  묶음(연출 = 장면, 자동 = 날짜)은 통째로 한쪽에만 간다.
- 학습 기본값: LoRA r=16·α=32, 언어 모델 attention 투영(`--mlp` 로 MLP 도), lr 1e-4,
  3 epoch, 배치 1 × 누적 8, gradient checkpointing, 픽셀 상한 VGA(640×480).
- 운용 반영: 개체 덮어쓰기 `config/devices/<기체>.local.yaml` 에서 `vision.vlm.model_id` 를
  병합 폴더의 **절대 경로**로 준다. 평가가 이 설정 경로 그대로 세션을 만들어 잰다.

## VRAM (추정 — 실측 전)

bf16 가중치 약 4.4GB(실측 운용 4.11GB) + LoRA·AdamW 상태 수십 MB + VGA 한 장(시각 토큰 약
400개)의 활성값. gradient checkpointing 으로 RTX 3080 10GB 에 들어갈 것으로 본다. 넘치면
`--max-pixels` 를 낮춘다(병합 폴더의 프로세서가 같은 상한을 운용으로 잇는다). 학습 끝에
최대 VRAM 을 출력하고 `train_config.json` 에 `peak_vram_gib` 로 남긴다.
