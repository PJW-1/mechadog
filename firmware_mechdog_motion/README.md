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

## 실제 구동 빌드

실제 구동에는 Hiwonder가 배포한 Arduino 예제의 `HW_MechDog.*`, `Hiwonder.*`,
`Servo.*`, `pwm_servo.*`, `WMMatrixLed.*`, `action.h`와 공식 `MechDog_Arduino`,
`MPU6050` 라이브러리가 필요하다. 배포 파일의 라이선스가 명확히 확인되지 않은
소스는 저장소에 복사하지 않고 로컬 빌드 디렉터리에서만 결합한다.

구동 빌드에서는 `MECHADOG_ENABLE_ACTUATORS=1`로 바꾼다. 업로드하면
`MechDog_init()`이 서보를 초기화하므로 처음에는 몸통을 받쳐 네 발을 띄운 상태에서
시험한다. 보정값은 `mechdog` NVS namespace에서 읽으며 업로드 전 별도 백업한다.

## PC 시험 명령

```powershell
python tools/udp_probe.py 192.168.1.100 --count 100 --interval 0.05
python tools/mechdog_command.py 192.168.1.100 safety
python tools/mechdog_command.py 192.168.1.100 move --step 10 --duration 0.5
python tools/mechdog_command.py 192.168.1.100 watchdog --step 10 --duration 0.4
```

`move`는 10Hz로 명령을 보내고 마지막에 `STOP`을 보낸다. `watchdog`은 마지막
`MOVE` 뒤 송신을 450ms 중단하고 SAFE 잠금과 failsafe 카운터 증가를 확인한다.
