# MechDog motion firmware

몸체 ESP32가 UDP 5001번 포트로 JSON 명령을 받고 Hiwonder 보행 API를 호출한다.
명령 형식의 정본은 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md)다.

## 안전 동작

- 부팅 직후에는 SAFE 잠금 상태이며 `MOVE`를 실행하지 않는다.
- 복구할 때는 `STOP` → `RESET_SAFE` 순서로 보내고, 그 뒤 새 `MOVE`부터 실행한다.
- `ESTOP`은 즉시 정지하고 SAFE를 잠근다.
- 정상적으로 파싱된 명령이 300ms 동안 없으면 정지하고 SAFE를 잠근다.
- 기형 JSON, 모르는 타입과 중복·역전 `seq`는 워치독 시간을 갱신하지 않는다.
- Wi-Fi가 끊기면 즉시 정지하고 SAFE를 잠근다.

`PING ...`에는 `ACK PING ...`으로 응답하지만 진단 패킷이므로 워치독 시간을
갱신하지 않는다.

## 기본 빌드

저장소의 기본값은 `MECHADOG_ENABLE_ACTUATORS=0`이다. 이 빌드는 네트워크,
파서와 안전 상태만 검증하고 서보를 초기화하지 않는다.

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion
arduino-cli upload -p COM9 --fqbn esp32:esp32:esp32 firmware_mechdog_motion
```

Wi-Fi 접속 정보는 ESP32 NVS에 이미 저장된 값을 `WiFi.begin()`으로 재사용한다.
SSID와 비밀번호를 저장소에 넣지 않는다.

## 실제 구동 빌드 — 벤더 파일을 직접 받아야 한다

**저장소에 없는 파일이 필요하다.** Hiwonder 예제 소스에 라이선스 표기가 없어 재배포할 수
없기 때문이다 ([ADR-20](../docs/DECISIONS.md)). 각자 공식 설치본에서 추출한다.

### 받는 곳

**공식 설치 프로그램 `MechDog V1.3`** — [Hiwonder Wiki · Resources Download](https://wiki.hiwonder.com/projects/MechDog/en/latest/docs/8.Resources_Download.html)
에서 받아 설치하면 Arduino 예제와 라이브러리가 함께 풀린다. 이 문서의 파일 목록은 **V1.3 기준**이다.

### 둘 곳 — 두 군데다

**① 예제 소스 11개 → 스케치 폴더에 그대로 복사**

```
firmware_mechdog_motion/
├── firmware_mechdog_motion.ino
├── HW_MechDog.cpp   HW_MechDog.h      ← 여기부터 벤더 파일
├── Hiwonder.cpp     Hiwonder.h
├── Servo.cpp        Servo.h
├── WMMatrixLed.cpp  WMMatrixLed.h
├── pwm_servo.cpp    pwm_servo.h
├── action.h
└── src/                               ← 우리 파일 (건드리지 않는다)
    ├── command_parser.*
    └── motion_hal.*
```

⚠️ **`src/` 안에 넣지 않는다.** Arduino 는 스케치 폴더의 `.cpp` 만 자동 컴파일한다.

**② Arduino 라이브러리 2개 → 라이브러리 폴더**

```
<Arduino 스케치북>/libraries/
├── MechDog_Arduino/     quad_kinematics.h · mech_base_types.h · precompiled 바이너리
└── MPU6050/             MIT (Copyright ⓒ 2019 ElectronicCats)
```

스케치북 위치는 Arduino IDE 의 `File → Preferences → Sketchbook location` 에 있다.
기본값은 Windows 에서 `%USERPROFILE%\Documents\Arduino` 다.

> `HW_MechDog.h` 가 `quad_kinematics.h`·`mech_base_types.h`·`MPU6050.h` 를 다시 부르므로
> **①만 복사하면 빌드되지 않는다.** ②까지 있어야 한다.

### 빌드

`src/motion_hal.h` 의 기본값을 바꾼다.

```cpp
#define MECHADOG_ENABLE_ACTUATORS 1
```

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion
arduino-cli upload -p COM9 --fqbn esp32:esp32:esp32 firmware_mechdog_motion
```

**파일이 없으면 컴파일이 안내와 함께 멈춘다** — `HW_MechDog.h 가 없습니다 …` 라는
`#error` 가 나오면 위 ①을 빠뜨린 것이다.

⚠️ **업로드하면 `MechDog_init()` 이 서보를 초기화한다.** 처음에는 몸통을 받쳐 **네 발을 띄운
상태**에서 시험한다. 보정값은 `mechdog` NVS namespace 에서 읽으며, 업로드 전에
순정 펌웨어 백업(H1)과 보정값을 따로 남긴다 — [하드웨어 1절](../docs/HARDWARE.md).

### 왜 저장소에 넣지 않는가

| 대상 | 라이선스 |
| :--- | :--- |
| 예제 소스 11개 | **표기 전무.** 라이선스 파일도 소스 헤더 저작권 문구도 없다 → 재배포 불가 |
| `MechDog_Arduino` | `license.txt` 는 BSD 인데 **저작권자가 Adafruit** 이다 — 껍데기와 함께 딸려온 파일로 보여 근거로 쓸 수 없다 |
| `MPU6050` | MIT — 명확하다 |

명시된 라이선스가 없으면 기본값은 저작권자 전권이다. 상세는 [ADR-20](../docs/DECISIONS.md).

## PC 시험 명령

```powershell
python tools/udp_probe.py 192.168.1.100 --count 100 --interval 0.05
python tools/mechdog_command.py 192.168.1.100 safety
python tools/mechdog_command.py 192.168.1.100 move --step 10 --duration 0.5
python tools/mechdog_command.py 192.168.1.100 watchdog --step 10 --duration 0.4
```

`move`는 10Hz로 명령을 보내고 마지막에 `STOP`을 보낸다. `watchdog`은 마지막
`MOVE` 뒤 송신을 450ms 중단하고 SAFE 잠금과 failsafe 카운터 증가를 확인한다.
