# WonderEcho 음성모듈 테스트 가이드

> 팀원용: 펌웨어 플래시부터 인식·소리·로봇 연결 테스트까지 혼자 따라할 수 있습니다.
> 막히면 로그를 그대로 캡처해서 공유해주세요.

## 0. 한눈에 보는 현재 상태

| 항목 | 상태 |
|---|---|
| 공장 펌웨어: 음성 인식·응답·로봇 4핀 | ✅ 동작 (장시간 방치 시 간헐 무음/무반응 버그 있음) |
| 커스텀 `38-pdm.bin` | 🆕 최신 — 무음 원인(PDM 전원) 수정, 실기 검증 대상 |
| 모듈↔로봇 I²C 통신 | 커스텀 v34+ 에서 복구됨 (0x34 ACK 실측) |
| PC 파이프라인 (Whisper→EXAONE→Piper) | 부가기능 — 모듈↔로봇 동작과 무관 |

## 1. 펌웨어 이미지 입수

빌드 산출물(`*.bin`)과 벤더 도구는 **이 저장소(공개)에 올리지 않습니다** — `.gitignore` + 벤더 재배포 금지(ADR-20) 정책 때문입니다.

| 무엇을 | 어디서 |
|---|---|
| 펌웨어 `.bin` 4종 + 플래시 도구 | 팀 공유 채널의 `wonderecho-음성모듈/` 패키지 (없으면 작업자에게 요청) |
| **소스 코드** | 이 폴더: `bridge/` (v34~v38 패치·소스·검증 기록), 상위 `*.c/h` (스트림 계열 전체 소스) |
| SDK·플래시 도구 원본 | Chipintelli `offline-speaker-1.12.16` SDK — 팀 공유본 또는 작업자 문의 |

| 파일 | 내용 |
|---|---|
| `00-factory.bin` | 공장 원본 — 기준선/원복용 (SHA256 `c4328480…`) |
| `38-pdm.bin` | **최신 커스텀** — v37 전체 + PDM 출력 전원 수정 (SHA256 `83dc8daf…`, 로그 `build=3801`) |
| `37-output-diag.bin` | 브리지+MP3+출력 계측 (PDM 누락으로 무음) |
| `27.bin` | WEC1 스트림 성공본 (9/14 소리 확인 — 레이아웃 다름 주의) |

### 소스 코드 위치 (저장소에 있음)

- `bridge/README.md` — v34 4핀 브리지 복구: 변경 내용·검증·한계
- `bridge/sdk-integration.patch` — SDK에 적용하는 통합 패치
- `bridge/diagnostics/` — v35 진단 계측 (`v35-from-v34.patch` + 스냅샷)
- `bridge/mp3-fix/` — v36 MP3 디코더 복원 (`v36-from-v35.patch`)
- `bridge/pdm-output/` — **v37/v38 최신**: 출력 계측 + PDM 전원 수정, `v38-src/`에 실제 빌드된 소스 스냅샷
- `bridge/robot-mapping.patch` — 로봇 측 `voice_dispatch` 방향 ID 정정 (로봇에 미적용 — 적용 전까지 구동 차단 상태에서만 테스트)
- 상위 `main.c`, `voice_stream.c`, `user_config.h` 등 — WEC1 스트림 계열(v27) 소스. 로그 빌드 계열(v34~38)과 다른 계열이니 `pdm-output/v38-src/`를 기준으로 볼 것

### 직접 빌드하려면

1. `offline-speaker-1.12.16` SDK + CI1302 GCC 툴체인 필요 (팀 공유본)
2. `bridge/pdm-output/v38-src/` 5개 파일을 SDK `projects/offline_asr_sample/src/`에 덮어쓰기
3. `bridge/`의 `build_*.py`, `package_*.py` 참조 — 경로는 `<…>` 플레이스홀더로 정리돼 있으니 본인 환경에 맞게 수정

## 2. 플래시 방법 (전 버전 공통)

1. 모듈 USB → PC 연결 (**로봇 4핀은 분리** — 이중 5V 급전 금지)
2. `PACK_UPDATE_TOOL.exe` → 칩 **CI1302** · COM 모듈 포트 · Baud **115200** · **EraseNV 해제**
3. `.bin` 선택 → Update → 완료 후 도구 종료(포트 점유 해제)
4. 포트 확인: 모듈 보통 **COM5**, 로봇 **COM8**

## 3. 테스트 절차

### 3-1. 기본 동작
1. **"Hello Hiwonder"** → 웨이크 (LED 반응)
2. **"Hello"** → 명령 인식 → 응답 소리 청취

로그 확인 (COM5 @921600, 읽기 전용):
```bash
python -c "import serial,time;s=serial.Serial('COM5',921600,timeout=0.3);e=time.monotonic()+15
while time.monotonic()<e:
 d=s.read(s.in_waiting or 1);print(d.decode('utf-8','replace'),end='')"
```

| 로그 | 의미 |
|---|---|
| `send result:HELLO-HI-WONDER` | 웨이크 인식됨 |
| `play start` → `play end` | 재생 완주 |
| `wave fmt err` / `receive_data err` | 음원 형식 오류 (구버전 증상) |
| `[WE-STAT] build=3801` | 커스텀 38 빌드 확인 (10초 주기 상태 로그) |
| `[WE-BRIDGE] build=3801 ready` | 브리지 초기화 성공 |
| 태스크 목록에 `cwsl_manage_tas` | 공장 펌웨어 식별자 |

### 3-2. 로봇 4핀 연결 테스트

**순서 중요:**
1. 모듈 USB 분리 (전원 완전 차단)
2. 모듈 4핀 → 로봇 연결
3. 로봇만 USB로 PC 연결 → 로봇이 모듈에 전원 공급
4. 로봇은 **SERVICE/parked 상태** 유지 — 로봇 측 `voice_dispatch` 방향 매핑(2/3/4) 수정이 아직 미적용이라 **구동 차단 상태에서 통신만 확인**

로봇 버스에서 모듈 확인: 로봇 API `GET /i2c/live?bus=0&from=0x34&to=0x34` → `0x34` 응답이면 연결됨.

### 3-3. PC 음성 테스트 도구 (선택)

`dev/wonderecho-audio/voice_tool.py` (GUI):
- 스피커 테스트: 펌웨어 자동 감지 (스트림=WEC1 톤 / 로그=음성 유도 테스트)
- 모듈 로그 보기: COM5 로그 10초 수집
- 파이프라인: Whisper STT + EXAONE LLM + Piper TTS 대화 (GPU 필요)

## 4. 알려진 문제

| 증상 | 원인 | 상태 |
|---|---|---|
| 소리 나다가 방치 후 무음/무반응, 리부팅으로 복귀 | `pause_asr` 카운터 스턱 (재생 콜백 유실→ASR 영구 차단) | 커스텀은 워치독 설계 보유, 공장본은 원본 버그 |
| 커스텀 빌드 디지털 완주·물리 무음 | **PDM 블록(HPOUT 드라이버) 전원 미인가** — 로그 빌드가 전원 경로를 통째로 제외 | `38-pdm`에서 수정 |
| `wave fmt err` | MP3 디코더 꺼짐(≤v35) / 파티션 +16KB 밀림(32fix) | v36·38 해결 |
| 로봇이 모듈 못 봄 | UART1 브리지 비활성 + 잘못된 버스 송신 | v34+ 해결 |

## 5. 트러블슈팅

| 증상 | 확인 |
|---|---|
| 플래시 도구 포트 못 엶 | COM5 점유 프로그램(로그 캡처·voice_tool) 종료 |
| 플래시 후 무반응 | USB 재연결(전원 사이클) 후 10초 대기 |
| LED 반응·소리 없음 | 무음 버그 — 전원 사이클로 복구 여부 확인 |
| 인식 자체 안 됨 | 귀먹음 상태 — 재부팅 후 재시도 |
| 로봇 명령 무반응 | 4핀 결선·로봇 IIC1 0x34 스캔 확인 |
| 소리 작음 | "Maximum volume" 음성 명령 |

## 6. 테스트 보고 양식

```
[일시] [펌웨어] (예: 38-pdm)
[연결] 모듈 USB 단독 / 로봇 4핀
[한 것] (예: Hello Hiwonder → Hello)
[결과] 소리/LED/로봇 반응
[로그] 있으면 첨부
```

---
*작성 2026-09-20. 상세 이력: 프로젝트 `04_작업일지/2026-09-19.md`·`2026-09-20.md` 참조.*
