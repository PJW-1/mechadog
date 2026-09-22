# WonderEcho 환경 재현 — AI 에이전트 실행 가이드

> **이 문서의 사용법**: 이 파일을 AI 에이전트(Claude/Codex/Devin 등)에게 통째로 전달하세요.
> AI가 아래 절차를 순서대로 실행해 환경 세팅 → 빌드 → 플래시 → 검증까지 진행합니다.
> 사람은 **"사람이 준비할 것"** 섹션의 물리적 준비물만 제공하면 됩니다.

---

## 0. AI 에이전트를 위한 지침

- OS: Windows 10/11. 셸은 **PowerShell 또는 Git Bash 하나로 통일** — 섞지 않는다.
- 경로에 한글·공백이 있을 수 있다. 항상 큰따옴표로 감싼다.
- 각 단계 끝의 **[검증]** 을 반드시 확인하고, 실패 시 §7 트러블슈팅을 본다.
- 되돌리기 어려운 작업(파일 덮어쓰기, 펌웨어 플래시) 전에 사용자에게 한 번 확인한다.
- COM 포트 번호는 PC마다 다르다. 기억된 번호를 신뢰하지 말고 매번 확인한다.

---

## 1. 사람이 준비할 것 (AI가 대신 못 하는 것)

| 준비물 | 용도 | 없으면 |
|---|---|---|
| WonderEcho 모듈 + USB 데이터 케이블 | 플래시·테스트 | 진행 불가 |
| **SDK 번들** (아래 §2-가 구조) | 빌드·플래시 도구 | §5 플래시 전용 경로는 가능 |
| 펌웨어 `.bin` 파일 | 바로 테스트 | 직접 빌드 경로(§4)로 대체 가능 |
| (선택) MechDog + 4핀 케이블 | 로봇 연동 시험 | 모듈 단독 시험은 가능 |

### 2-가. SDK 번들 구조 (팀 공유본을 어디에 두든 이 구조여야 함)

```
<SDK_ROOT>/                              ← 아무 폴더나 가능, 예: C:\dev\voice-sdk
├── offline-speaker-1.12.16/             ← Chipintelli SDK 본체
│   ├── projects/offline_asr_sample/     ← 빌드 대상 프로젝트
│   ├── tools/ci-tool-kit.exe            ← 펌웨어 패키징 도구
│   ├── tools/PACK_UPDATE_TOOL.exe       ← GUI 플래셔
│   ├── tools/build-tools/bin/make.exe   ← 빌드 도구
│   ├── libs/libfbin.a
│   ├── driver/  components/  system/
│   └── ...
├── toolchain/gcc_fix_raissrc/bin/       ← Nuclei RISC-V GCC
│   └── riscv-nuclei-elf-gcc.exe 등
└── recovery/
    └── 2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin
        ← 공장 펌웨어 원본 (asr/dnn/voice/user 리소스의 출처)
        SHA256: c4328480d8f9cbe2e15dde41bb7d170d93c46c96c884742394322db87b5d2b2d
```

> SDK 번들은 벤더(Chipintelli/Hiwonder) 자산이라 이 저장소에 없습니다.
> 팀 공유 채널 또는 작업자에게 받으세요. 없으면 §5(이미 만들어진 .bin 플래시)만 가능합니다.

---

## 2. 환경 확인 (AI가 실행)

```powershell
python --version            # 3.10+ 권장
pip install pyserial        # 시리얼 로그/통신
git --version
```

저장소 클론(아직 안 했다면):

```bash
git clone https://github.com/PJW-1/mechadog.git
```

**[검증]** `experiments/wonderecho-audio/` 폴더에 `TESTING.md`, `bridge/` 가 보여야 함.

---

## 3. 경로 변수 설정 (AI가 먼저 잡는다)

```powershell
$env:WE_SDK   = "C:\dev\voice-sdk"                 # SDK 번들 루트 (실제 위치로)
$env:WE_REPO  = "C:\dev\mechadog"                  # 클론한 저장소
$env:WE_OUT   = "C:\dev\voice"                     # 산출물 폴더 (없으면 생성)
```

**[검증] 아래 4개가 모두 존재해야 빌드 가능:**

```powershell
Test-Path "$env:WE_SDK\offline-speaker-1.12.16\projects\offline_asr_sample"
Test-Path "$env:WE_SDK\toolchain\gcc_fix_raissrc\bin\riscv-nuclei-elf-gcc.exe"
Test-Path "$env:WE_SDK\offline-speaker-1.12.16\tools\ci-tool-kit.exe"
Test-Path "$env:WE_SDK\recovery\2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin"
```

공장 원본 무결성 확인:

```powershell
(Get-FileHash "$env:WE_SDK\recovery\2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin" -Algorithm SHA256).Hash
# 기대값: C4328480D8F9CBE2E15DDE41BB7D170D93C46C96C884742394322DB87B5D2B2D
```

---

## 4. 소스 적용 + 빌드 (최신 v38 = `38-pdm.bin` 재현)

> 목표: 브리지 + MP3 + 출력 계측 + **PDM 출력 전원**이 들어간 최신 커스텀.
> 로그에 `build=3801`이 찍혀야 정상.

### 4-1. v38 소스 덮어쓰기

```powershell
$src = "$env:WE_REPO\experiments\wonderecho-audio\bridge\pdm-output\v38-src"
$dst = "$env:WE_SDK\offline-speaker-1.12.16\projects\offline_asr_sample\src"
# 원본 백업 먼저!
Copy-Item "$dst\main.c","$dst\user_config.h","$dst\user_msg_deal.c" "$dst\" -Destination "$dst\backup-before-v38\" -Force -ErrorAction SilentlyContinue
mkdir "$dst\backup-before-v38" -Force | Out-Null
Copy-Item "$dst\*.c","$dst\*.h" "$dst\backup-before-v38\" -Force
Copy-Item "$src\*" $dst -Force
```

> 참고: v38-src는 최소화 로그 빌드 계열이다. 스트림(WEC1) 기능은 없다.
> 상세 계보: `bridge/README.md`, `bridge/pdm-output/README.md`.

### 4-2. 빌드

```powershell
$env:PATH = "$env:WE_SDK\toolchain\gcc_fix_raissrc\bin;$env:WE_SDK\offline-speaker-1.12.16\tools\build-tools\bin;$env:PATH"
cd "$env:WE_SDK\offline-speaker-1.12.16\projects\offline_asr_sample\project_file"
mkdir build -Force | Out-Null
make.exe PROJECT_NAME=offline-stream -f makefile -f strict_stream.mk -j6 build/offline-stream.elf
riscv-nuclei-elf-size.exe build\offline-stream.elf
```

**[검증]** `build/offline-stream.elf` 생성, text 영역 ~90KB 미만 (code 슬롯 167,936B 이내여야 함).

### 4-3. 패키징 (공장 리소스 보존형)

공장 `asr/dnn/voice/user` 파티션은 그대로 두고 user code만 교체한다.
자동화 스크립트는 `bridge/package_bridge.py` / `mp3-fix/package_mp3_fix.py` 참조 —
경로 플레이스홀더(`<WE_SDK_TREE>` 등)를 본인 환경 변수로 바꿔서 사용.

핵심 흐름(요약):
1. `objcopy -O binary elf → [0]code.bin`, `libfbin.a → [1]code.bin`
2. `ci-tool-kit merge user-file` → `user_code.bin`
3. 공장 bin에서 `boot/asr/dnn/voice/user` 파티션 추출
4. `ci-tool-kit mf` 로 최종 이미지 생성 (인자 형식은 `package_bridge.py` 참조)
5. **파티션 오프셋 검증 필수**: asr=184320, dnn=262144, voice=1441792, user=1957888

**[검증]** 출력 이미지 크기 정확히 **1,969,737B**, 위 오프셋 일치.

---

## 5. 플래시 (사람 + AI 협업)

1. 모듈 USB → PC (**로봇 4핀은 반드시 분리** — 이중 5V 금지)
2. 사람: `PACK_UPDATE_TOOL.exe` 실행 → **CI1302** · 모듈 COM 포트 · **115200** · **EraseNV 해제** → `.bin` 선택 → Update
3. 완료 후 도구 종료 (포트 점유 해제)

COM 포트 확인:

```powershell
python -c "from serial.tools import list_ports; [print(p.device, p.description) for p in list_ports.comports()]"
```

---

## 6. 검증 (AI가 수행)

모듈 로그 읽기 (UART0 @921600, 읽기 전용):

```powershell
python -c "import serial,time;s=serial.Serial('COM5',921600,timeout=0.3);e=time.monotonic()+15
while time.monotonic()<e:
 d=s.read(s.in_waiting or 1);print(d.decode('utf-8','replace'),end='')"
```

| 로그 | 판정 |
|---|---|
| `[WE-BRIDGE] build=3801 ready` | ✅ v38 커스텀 부팅 성공 |
| `[WE-STAT] build=3801 tick=…` (10초 주기) | ✅ 계측 동작 |
| `cwsl_manage_tas` 태스크만 보임 | 공장 펌웨어 — 커스텀 아님 |
| 부팅 후 **"Hello Hiwonder"** 발화 → `send result:HELLO-HI-WONDER` | ✅ 인식 |
| 이어서 `play start` → `play end` + **실제 소리** | ✅ 재생 (소리는 반드시 귀로 확인) |
| `wave fmt err` | 파티션/MP3 문제 — §7 참조 |

PC 스피커로 대신 말하게 할 수도 있음 (모듈 마이크가 들음):

```powershell
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = -2; $s.Volume = 100
$s.Speak("Hello Hiwonder")   # 모듈 근처에서 재생
```

### 로봇 4핀 연동 (보수적 절차)

1. 모듈 USB 완전 분리 → 4핀을 로봇에 연결 → 로봇만 USB 전원
2. 로봇은 **SERVICE/parked 상태** — 저장소의 ESP32 펌웨어에는 WonderEcho 명령 소비 코드가 없으므로 **구동 차단 상태에서 버스 연결만** 확인
3. OTA 진단 빌드와 비공개 인증 설정이 있을 때만 인가된 `/i2c/live` 요청으로 `0x34` 응답 확인. 기본 OTA-OFF 빌드에서는 되돌릴 이미지를 확보한 뒤 별도 `diagnostics/i2c_scan` 스케치를 사용

---

## 7. 트러블슈팅

| 증상 | 원인 후보 | 조치 |
|---|---|---|
| make 실패 — toolchain 명령 없음 | PATH 누락 | §3 PATH 재설정 |
| 링크 overflow / 이미지 >1,969,737B | code 슬롯 초과 | 스트림 코드 포함 여부 확인 — v38-src는 최소 계열 |
| 플래시 도구가 포트 못 엶 | 로그 캡처/다른 프로그램 점유 | 전부 종료 후 재시도 |
| `wave fmt err` | MP3 디코더 꺼짐 or 파티션 밀림 | `user_config.h` MP3 플래그·오프셋 검증 |
| 인식되는데 소리 없음 | PDM 전원 미인가(v37 이하 증상) | `main.c`에 `pdm_power_up` 있는지 확인 → v38인지 |
| 소리 나다가 방치 후 무반응 | 공장 `pause_asr` 스턱 버그 | 전원 사이클, 재발 시 로그 공유 |
| 로봇이 음성 명령으로 움직이지 않음 | ESP32 WonderEcho 소비 코드 미구현 | 4.7.9 구현 전에는 버스 연결만 검증 |

---

## 8. 참고 문서 (저장소 내)

- `TESTING.md` — 팀원 테스트 절차·버전 설명·보고 양식
- `bridge/README.md` — v34 브리지 상세
- `bridge/pdm-output/README.md` — v37/v38 상세
- `bridge/check_robot_mapping.py` — 저장소 밖 벤더 dispatcher 스냅샷용 수동 검사

*작성 2026-09-20*
