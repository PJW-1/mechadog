# WonderEcho 음성 파이프라인 (WBS 4.7.4~4.7.6 · 4.7.14)

WonderEcho V3.0(CI1302) 모듈을 **마이크·스피커 주변기기**로 쓰고, 판단은 전부 PC에서
하는 로컬 음성 대화 스택이다. 모듈 펌웨어는 커스텀 빌드(`firmware/`, 현재 설치본
v27)가 필요하다.

```
모듈 마이크 ──UART──▶ faster-whisper STT ──▶ 로컬 GGUF LLM ──▶ Piper TTS ──UART──▶ 모듈 스피커
                                              ▲
                              knowledge/*.txt 검색 주입 (경량 RAG)
```

## 실행

```powershell
.\start_voice.ps1            # 대화 + 관제 API(:8090) — COM5, Whisper medium, EXAONE 7.8B
python voice_pipeline.py --port COM5 --say "문장"   # 단발 재생 (모델 로드 없음)
```

- 웨이크워드: "메카독, …"으로 시작해야 응답. "그만/대기해"→대기 모드(종료 아님),
  "메카독 시작/깨워/일어나"→복귀, "메카독 도와줘/비상"→`emergency_log.txt` 기록+안내.
- 전체 종료는 `Ctrl+C`만. 플래시 후엔 모듈 RST를 한 번 눌러야 한다(자동 부팅 무음).

## 관제 API (`--web 포트`, 기본 예시 8090)

시리얼 링크는 메인 루프가 단독 소유하고, 웹 요청은 큐로 넣어 턴 사이에 순차
재생된다 — 프레임이 섞이지 않는다.

| 경로 | 기능 |
| --- | --- |
| `GET /status` | `{robot, mode, activity, say_queue, events}` — 듣는 중/생각 중/말하는 중 브리핑 |
| `POST /say {text, urgent}` | 관제 공지 → 로봇 방송. 대기 모드에서도 나감, `urgent`는 큐 맨 앞 |
| `POST /mode {mode}` | `active`/`standby` 명시 전환(생략 시 토글) |
| `GET /transcript` | 최근 발화 로그(user/robot/admin/system) |
| `GET /` | 내장 테스트 패널 — 관제웹 연동 전 단독 시연용 |

로컬 출처(`127.0.0.1`/`localhost`)만 CORS 허용. 로봇별로 `--robot-id`와 포트를
나누면 관제웹에서 다중 기체를 모아 볼 수 있다.

## knowledge/ — 가상 회사 데이터 (전부 데모용)

14개 문서는 **실제 회사 자료가 아닌 합성 샘플**이다. 파일을 실제 문서로 교체하면
그대로 동작한다. LLM은 문서에 없는 내용을 "확인되지 않은 정보"로 답하게 프롬프트로
강제한다 — 생산량·명단 같은 권위 있는 판단을 LLM이 지어내지 않는다.

## 전송층 — COM5는 임시다

USB 시리얼(`--port COM5`)은 검증용 임시 링크다. 목표 경로는
`모듈 ↔ 로봇 4핀 I²C ↔ ESP32 ↔ Wi-Fi`(WBS 4.7.9)이며, 그 위의 루프·검색·웹 API는
전송층과 무관하게 그대로 쓴다. 링크 소유 규칙(하나의 소유자가 오디오 프레임을
직렬화)은 소켓으로 바뀌어도 동일하다.

## firmware/ — 커스텀 펌웨어 소스 (참조용 스냅샷)

벤더 SDK(`CI130X SDK` 트리, 미포함)의 `projects/offline_asr_sample` 위에 겹쳐 쓰는
변경 파일들이다. 현재 설치본 **v27(태그 238)** 의 내용:

- `voice_stream.c`: PLAY_DATA(0x110)/PLAY_END(0x111) 수신, 48KB 재생 풀,
  페이로드 체크섬 검증(`rx_bad` 진단 카운터)
- `voice_prompt.c`: 부팅 시 코덱 무음 예열 1.5s — 첫 재생 앞머리 손실 제거
- `main.c`, `user_config.h`: 스트림 모드 진입·보드 설정

PCM 형식: 16kHz mono PCM16, 프레임 `A5 A5 5A 5A` + 16B 헤더(체크섬 포함), 921600bps.
펌웨어 플래시는 115200bps.

## 시험

```powershell
python -m pytest test_protocol.py test_stream_client.py -q
```

## 부산물/비포함

모델 가중치(GGUF·piper onnx·whisper), SDK 트리, `*.wav`, `emergency_log.txt`는
커밋하지 않는다. 실행 요구 패키지는 `requirements-pc.txt` + faster-whisper,
llama-cpp-python, piper-tts, av.
