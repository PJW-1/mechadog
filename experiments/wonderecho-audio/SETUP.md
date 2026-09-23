# PC 환경 구축 — 음성 인식·합성

**DB 설치 정본:** [DB_GUIDE.md](DB_GUIDE.md)의 음성3테이블 + 규칙 JSON 구성이다.
팀 합성 자료는 `python prepare_demo.py --output-dir .`로 만들고
`python voice_store.py --check-schema`로 확인한다. 기존 음성8테이블 DB는 먼저
`python migrate_voice_db.py --db voice_data.db`로 백업·이전한다.
추가 응답은 `phrases` 테이블에만 저장하며 `phrases_custom.json`은 구버전 이전 입력이다.

WonderEcho 모듈이 마이크와 스피커를 맡고, **판단은 전부 이 PC가 한다.** 모듈에서는 한국어 인식을 하지 않는다 — 모듈 CPU로는 불가능하고, 인식은 PC의 GPU가 한다.

> **개정 (2026-09-23 · [ADR-38](../../docs/DECISIONS.md#adr-38))** — 음성 경로는 규칙만 쓴다. 로컬 LLM(GGUF · llama-cpp)은 폐기했으므로
> 대화 모델을 받을 필요가 없고, 가상 MES 서버(`:8095`)도 없어졌다. 파이프라인에 필요한 모델은 faster-whisper(1절)와
> Piper 음성(`--piper-model`)뿐이다. WonderEcho COM 링크는 임시이며, 목표 구조는 XIAO 마이크(`:82/audio`) 듣기 ·
> 로봇 MP3 모듈(I²C `0x7B`) 말하기다(4.7.19 는 완료, 4.7.20~4.7.21 미착수).

```
[음성 모듈]                                   [PC]
 마이크 → 코덱 ADC → Speex 압축(3 KB/s) ──→ faster-whisper medium (CUDA)
                                            → "사원 홍길동입니다."
                                            → 이름 추출
 스피커 ← 코덱 DAC ← TC8002D 앰프  ←──────  재생 명령(인덱스)
```

모델 파일은 이 저장소에 없다. 합쳐서 약 15 GB이고 GitHub 파일 크기 제한을 넘으며, Orpheus는 게이트 저장소라 재배포가 금지된다. 아래 절차대로 각자 내려받는다.

---

## 0. 미리 확인할 것

| 항목 | 최소 조건 | 확인 방법 |
|---|---|---|
| OS | Windows 10/11 | — |
| Python | 3.12 | `py -0p` |
| GPU | NVIDIA, VRAM 8 GB 이상 | `nvidia-smi` |
| 디스크 | 20 GB 이상 여유 | — |

GPU가 없으면 인식은 CPU로도 돌아가지만 느리다. 음성 **합성**(Orpheus 3B)은 사실상 GPU가 필요하다.

### GPU compute capability 확인 — 이게 PyTorch 빌드를 결정한다

```bash
nvidia-smi --query-gpu=name,compute_cap --format=csv
```

| compute_cap | 아키텍처 | 필요한 CUDA 빌드 |
|---|---|---|
| 8.6 / 8.9 | Ampere / Ada | cu121 이상 |
| **12.0** | **Blackwell (RTX 50 시리즈)** | **cu128 이상** |

**RTX 50 시리즈에 cu121을 깔면 `no kernel image is available` 로 실패한다.** 이 프로젝트는 RTX 5070(compute_cap 12.0)에서 cu128로 검증했다.

---

## 1. 인식용 환경 (faster-whisper)

인식은 CTranslate2 기반이라 **PyTorch가 필요 없다.** 가볍게 따로 만든다.

```bash
py -3.12 -m venv C:\dev\mechadog-voice\pc-tools
C:\dev\mechadog-voice\pc-tools\Scripts\python.exe -m pip install -U pip
C:\dev\mechadog-voice\pc-tools\Scripts\python.exe -m pip install -r requirements-stt.txt
```

### 모델 내려받기

```bash
C:\dev\mechadog-voice\pc-tools\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-medium', local_dir=r'C:\dev\mechadog-voice\models\faster-whisper-medium')"
```

약 1.5 GB. `medium` 이 한국어 정확도와 속도의 균형점이다. `small` 은 이름을 자주 틀리고 `large-v3` 는 VRAM을 많이 쓴다.

### CUDA DLL 주의

`faster-whisper` 는 `cublas64_12.dll` 등을 필요로 하는데, pip로 받은 `nvidia-*` 패키지 안에 들어 있고 **PATH에 자동으로 잡히지 않는다.** `transcribe_local.py` 의 `gpu_dll_directories()` 가 이 문제를 해결한다 — 전역 PATH를 건드리지 않고 해당 venv의 DLL 폴더만 등록한다. 직접 faster-whisper를 부르는 스크립트를 새로 쓴다면 그 함수를 재사용할 것. 그러지 않으면 이렇게 실패한다.

```
RuntimeError: Library cublas64_12.dll is not found or cannot be loaded
```

---

## 2. 합성용 환경 (Orpheus TTS) — 선택

안내 음원을 직접 만들 때만 필요하다. 이미 만들어진 음원을 쓴다면 건너뛴다.

**인식용 venv와 반드시 분리한다.** PyTorch가 4.9 GB이고, 검증된 인식 환경을 오염시키지 않기 위해서다.

```bash
py -3.12 -m venv C:\dev\mechadog-voice\tts-tools
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -m pip install -U pip
```

### PyTorch — CUDA 빌드를 명시할 것

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

`--index-url` 없이 설치하면 Windows에서는 CPU 전용 빌드가 깔린다. 위에서 확인한 compute_cap에 맞는 빌드를 쓴다.

설치 후 확인:

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_capability(0), torch.cuda.get_arch_list())"
```

출력의 `get_arch_list()` 에 자신의 `sm_<compute_cap>` 이 들어 있어야 한다. RTX 5070이면 `sm_120` 이 보여야 한다.

### 나머지 의존성

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -m pip install -r requirements-tts.txt
```

### Hugging Face 로그인 — Orpheus는 게이트 저장소다

`canopylabs/3b-ko-ft-research_release` 는 Llama-3.2-3B 파생이라 약관 동의가 필요하다. 자동 승인이지만 로그인은 있어야 한다.

1. https://huggingface.co/canopylabs/3b-ko-ft-research_release 에서 약관 동의
2. https://huggingface.co/settings/tokens 에서 read 토큰 발급
3. 로그인 — **토큰을 다른 사람이나 도구에 주지 말 것. 이 명령이 물어보면 붙여넣는다.**

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\hf.exe auth login
```

### 모델 내려받기 — 추론에 필요한 파일만

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -c "import os; os.environ['HF_HUB_CACHE']=r'C:\dev\mechadog-voice\models\hf-cache'; from huggingface_hub import snapshot_download; snapshot_download('canopylabs/3b-ko-ft-research_release', allow_patterns=['config.json','*.safetensors','model.safetensors.index.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json']); snapshot_download('hubertsiuzdak/snac_24khz')"
```

**`allow_patterns` 를 반드시 준다.** 이 저장소는 학습 체크포인트 전체를 담고 있어서, 그냥 받으면 `optimizer.pt` 19.8 GB를 포함해 33 GB를 내려받는다. 추론에는 쓰이지 않는다.

### 캐시 경로는 한 가지 방식으로만 지정할 것

`HF_HOME` 과 `cache_dir` 을 **동시에** 주면 안 된다. 중첩된 `from_pretrained` 호출이 `cache_dir` 을 물려받지 못해 캐시가 두 곳으로 갈라지고, 이렇게 실패한다.

```
OSError: Can't load feature extractor for '...\speech_tokenizer'
```

`HF_HUB_CACHE` 하나만 쓴다. `synth_prompt_orpheus.py` 는 `--cache-dir` 을 받으면 내부에서 `HF_HUB_CACHE` 로 바꿔 설정한다.

---

## 3. 동작 확인

### 인식 경로

모듈을 COM 포트에 연결한 뒤 (포트 번호는 자신의 환경에 맞춘다):

```bash
C:\dev\mechadog-voice\pc-tools\Scripts\python.exe -X utf8 stream_client.py --port COM5 --prompt
```

안내음이 나오고 5초 녹음이 이어진다. 안내가 끝나면 **"사원 OOO 입니다"** 형식으로 말한다.

측정 폴더가 `capture.json` · `uart.bin` · `voice.wav` 와 함께 생성된다. 그 폴더를 인식에 넘긴다:

```bash
C:\dev\mechadog-voice\pc-tools\Scripts\python.exe -X utf8 transcribe_local.py --model C:\dev\mechadog-voice\models\faster-whisper-medium <측정폴더>
```

정상이면 이렇게 나온다:

```json
{
  "language": "ko",
  "text": "사원 홍길동입니다.",
  "claimed_name": "홍길동",
  "identity_verified": false
}
```

`identity_verified` 가 항상 `false` 인 것은 의도된 것이다. **이름을 받아 적었을 뿐 신원을 인증한 것이 아니다.** 인증은 상위 시스템의 몫이다.

### 판정 기준

| 값 | 정상 | 이상할 때 의미 |
|---|---|---|
| `frames` | 250 | 5초 녹음이 끊겼다 |
| `checksum_errors` / `timeouts` | 0 | 통신 불량 |
| `no_speech_prob` | 0.3 미만 | **0.7 이상이면 무음에 대한 환각이다.** 아래 참조 |
| `claimed_name` | 이름 | `null` — 형식이 안 맞거나 다른 말이 섞였다 |

**무음일 때 Whisper는 그럴듯한 문장을 지어낸다.** 실제로 `"오늘도 시청해 주셔서 감사합니다."` 가 `no_speech_prob 0.753` 으로 나온 적이 있다. `no_speech_prob` 를 반드시 함께 볼 것.

### 합성 경로 (선택)

```bash
C:\dev\mechadog-voice\tts-tools\Scripts\python.exe -X utf8 synth_prompt_orpheus.py <출력폴더> --cache-dir C:\dev\mechadog-voice\models\hf-cache --seed 7 --temperature 0.4
```

**`--temperature` 를 0.6 이상으로 올리지 말 것.** 기본값 0.6에서는 "신원"을 "신호를"·"신앙을"로 잘못 발음했다. 0.4로 낮추면 3개 시드 × 2화자 모두 정확했다.

생성한 원본은 모듈이 받는 형식으로 변환해야 한다:

```bash
python -X utf8 build_prompt_audio.py <원본.wav> <출력.wav>
```

16 kHz 모노 16-bit로 맞추고, 앞뒤 비발화 구간을 잘라내고, 피크를 정규화한다. 모듈은 48~128,000 바이트만 받으므로 **약 4초가 상한**이다.

---

## 4. 알아둘 제약

### 음원 길이

모듈의 user 영역에 남은 공간은 실측 **53,248 바이트**다. 16 kHz 16-bit로는 약 1.66초분이다. 여러 문구를 넣으려면 ADPCM 압축이나 다른 전송 방식이 필요하다.

### 앰프 기동 지연

스피커를 구동하는 TC8002D는 셧다운 상태에서 깨어나는 데 시간이 걸린다. **음원 앞에 250 ms 무음을 넣지 않으면 첫 음절이 잘린다.** 실제로 "신원을"이 "인원을"로 들렸다. 벤더 SDK에도 `vTaskDelay(300); //等待功放开启`(앰프 켜지길 대기) 주석이 있다.

### 이름 인식 형식

`transcribe_local.py` 의 `claimed_name()` 은 **`사원 OOO 입니다` 형식만** 받아들이고, 그 외의 말이 한 마디라도 섞이면 거부한다. 애매한 답을 통과시키지 않기 위한 선택이다. "나 홍길동이야"는 인식되지 않는다.

---

## 5. 검증 기록

이 문서의 수치는 전부 실측이다.

| 항목 | 값 |
|---|---|
| 검증 GPU | RTX 5070 (compute_cap 12.0), 드라이버 610.88 |
| PyTorch | 2.11.0+cu128 |
| 인식 모델 | faster-whisper medium, CUDA float16 |
| 인식 소요 | 모델 적재 1.8초 + 전사 1.6초 |
| 합성 모델 | Orpheus 3B 한국어 (Apache 2.0 표기, Llama-3.2-3B 파생) |
| 스트림 대역폭 | 3,039 B/s (Speex, 5초에 15,198 바이트) |
| 통신 | 250프레임 / 5초, checksum·length·timeout 오류 0 |

한국어 인식 정확도는 별도로 재지 않았다. 위 수치는 특정 발화 한 건의 결과이며 일반적인 정확도 지표가 아니다.

---

## 6. 로컬 전용 자원 — 다른 환경에서의 재현

이 폴더의 실행 상태 일부는 **커밋되지 않는다** (`.gitignore`). 다른 PC·팀원·에이전트가 클론만으로는 같은 환경이 안 되며, 아래 절차로 재생해야 한다. 반대로 말하면 **이 자원 없이는 검증이 불가능한 항목이 있다** — 그 경우 테스트 결과를 "이 PC에서만 확인됨"으로 표시한다.

| 자원 | Git | 재생 방법 |
|---|---|---|
| `voice_data.db` | 무시됨 | `python prepare_demo.py --output-dir .` — 정본 음성3테이블 생성. 기존 DB는 보존 |
| `voice_data.rules.json` | 무시됨 | 같은 명령으로 공유 JSON의 고정 발화 규칙 재현 |
| `phrases_custom.json` | 이전 입력만 | `python migrate_voice_db.py --db voice_data.db`로 `phrases`에 이전·백업. 새 관리 API는 DB만 사용 |
| `voice_cache/` | 무시됨 | TTS 캐시 — 자동 생성 |
| `emergency_log.txt` | 무시됨 | 비상 발화 시 자동 생성 |
| Whisper 모델 폴더 | 미포함 | 1절 다운로드 절차 (모델 파일은 라이선스·용량으로 커밋하지 않는다) |
| Piper 모델 | 미포함 | `--piper-model` 로 지정하는 로컬 `.onnx`(기본 `ko_KR-kss-medium.onnx`) — 이 문서에 내려받기 절차는 없다 |
| Orpheus 모델 | 미포함 | 2절 다운로드 절차 (단독 안내음 제작용 — 파이프라인은 쓰지 않는다) |
| `knowledge/*.txt` | **커밋됨** | 단, 내용은 전부 **합성 데모 문서** — 실사 자료 아님 |

### 암구호(3.8.2)는 기본값이 코드에 있다

데모 문구 `"메카독 출입 허가"` 가 `voice_pipeline.DEFAULT_PASSPHRASES` 에 있다 — **실제 암구호는 코드가 아니라 DB에 넣는다:**

```bash
python voice_store.py --set auth_passphrases '["실제 암구호", "예비 문구"]'
```

키 이름이 `pass` 를 포함해 `--dump`·`--set` 에코에서 값이 자동으로 가려진다. 빈 목록(`'[]'`)으로 두면 음성 인증 경로 자체가 닫힌다.

공유용 `--seed`/`prepare_demo.py`는 공개 설정4개만 넣고 암구호를 자동 저장하지 않는다.
설정이 없는 데모의 코드 기본값과 관리자가 명시적으로 저장한 인증 설정을 구분한다.
암구호 설정이 들어 있는 DB는 `db_transfer.py export`가 거부하며 공유 자료로 반출하지 않는다.
정본 스키마는 PR223과 같은 3테이블이며, 인증 기능 때문에 구형8테이블을 되살리지 않는다.

### 다른 환경에서 검증 가능/불가능

| 검증 | 새 환경에서 가능? |
|---|---|
| `python -m unittest discover -s experiments/wonderecho-audio -p 'test_*.py'` (전체 단위시험, CI 와 같은 명령) | ✅ 가능 — 하드웨어·DB·모델 불요 (requirements-pc.txt 만 설치) |
| `/api/command/auth` FSM 전이·거절 | ✅ 가능 — `pytest tests/test_command_api.py` |
| STT 환각 거름·암구호 대조 로직 | ✅ 가능 — mock 기반 |
| 실제 음성 왕복 (모듈 마이크→인증 해제) | ❌ **WonderEcho 모듈 + COM 포트 + 모델 파일 필요** — 다른 환경에서는 재현 불가. 실기 결과는 "측정한 기체·날짜 한정"으로 표시한다 |
