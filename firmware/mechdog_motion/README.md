# MechDog motion firmware

몸체 ESP32가 UDP 5001번 포트로 JSON 명령을 받고 Hiwonder 보행 API를 호출한다.
명령 형식의 정본은 [`docs/PROTOCOL.md`](../../docs/PROTOCOL.md)다.

> **구동·센서 빌드를 하려면 먼저 아래 *"구동·센서 통합 빌드 준비"* 절을 본다.** 벤더 파일은
> 저장소에 없어 각자 받아야 하며(ADR-20), 준비 상태는 `python tools/dev/firmware_env.py` 로 점검한다.

선택 기능인 [정지 진단용 Wi-Fi 업데이트](OTA.md)는 **정지 상태 전용**이다.
구동 OFF 빌드에서는 항상 사용할 수 있고, 구동 빌드에서는 아래 SERVICE 모드로
로봇을 주차시킨 상태에서만 `/firmware`·`/confirm`을 받는다.
최초 파티션 전환/데이터 보존과 이후 HTTPS 업데이트 절차를 구분한다.

## 3.2.4 제어 루프 감시 — 정지 OTA 통합 (2026-09-13)

PC 쪽에서 실제로 어떤 순서로 검증했는지는
[루프 워치독 실물 검증 절차](../../docs/internal/WATCHDOG_VERIFICATION.md)에 정리돼 있다.
기체·빌드 고정값, 단계별 통과 기준, 안전 규칙, 사고 시 복구 절차를 포함한다.

**옵션 기본값은 OFF지만 검증된 정지 OTA 패키지는 명시적으로 ON이다.**
`MECHADOG_ENABLE_TASK_WDT=1`, `MECHADOG_ENABLE_OTA=1`, 센서 ON·구동 OFF 조합이다.
시험용 H 명령이 없는 최종본은 `MECHADOG_WATCHDOG_FAULT_PROBE=0`이다.
통합 시험본의 무한루프 주입 3회와 미확정 앱 롤백 시험 1회 모두 약 813ms 이내
ROM 재부팅 신호를 수신했다. 실제 보행·장시간 및 모든 고장 조건의 보장은 아니다.
SDK TWDT/IWDT의 설정·구독·갱신은 변경하지 않는다 — 공용 TWDT 기한을 5초에서 1초로
줄이면 Wi-Fi 기동 중 CPU0 Idle 감시 오류로 반복 재부팅한다(2026-09-12 실기).

- 제어 루프와 같은 코어에 `loop_monitor` 태스크 1개를 생성한다.
  우선순위는 `configMAX_PRIORITIES-2`(현재 23), 스택은 3072바이트+RTOS 관리 메모리다.
  SDK IPC보다 낮으며 10ms마다 RTOS 대기에서 깨어나 짧은 상태 확인만 한다.
  매 반복 할당·I/O·센서 접근·Host PC 처리는 없다. CPU 부하 개선을 주장하지 않는다.
- `setup()` 말미에서 시작하고 정상 제어 루프 말미에서만 진척 시각을 기록한다.
  감시 태스크가 제어 루프 대신 갱신하지 않으며 소유 태스크 외 호출은 거부한다.
  64비트 단조 시각과 짧은 임계영역으로 두 태스크의 상태 접근을 보호한다.
- 진척 없이 750ms가 지나면 오류를 래치하고 감시 태스크가 `esp_restart()`를 호출한다.
  늦게 도착한 진척은 오류를 지울 수 없다. 생성 실패·중복 시작·잘못된 호출은
  오류를 반환하며 본체 호출부는 이를 실패로 처리한다.
- **750ms는 진척 제한 설정이고 약 813ms는 UART로 관측한 재부팅 시간이다.**
  관측 간격 10ms 외에 스케줄링, 플래시 캐시 중단, SDK 종료/재시작 시간도 걸린다.
  한정된 시험 통과를 모든 조건의 최악 시간 보장으로 확대하지 않는다. CPU 인터럽트/스케줄러 자체가 멈추면
  이 태스크도 실행할 수 없으며 SDK 보호가 남지만 동일한 1초 보장은 아니다.
- 별도 센서 태스크만 멈추고 제어 루프가 계속 도는 상황 및 초기 setup 완료 전은
  이 진척 감시의 범위가 아니다. 센서 신선도와 600ms 명령 타임아웃은 별도 기능이다.
- **구동 빌드는 부팅 시 감시를 arm 하지 않는다.** 아래 SERVICE 모드로 몸을 주차한 뒤에만 arm 한다.
  정지 OTA는 별도 HTTPS 태스크에서 전송하며 루프 감시를 계속 유지한다.
  업데이트 중 감시 중지·대리 feed·기한 연장은 없다. 실제 정상 전송/확정,
  4096바이트 전송 후 연결 중단과 기존 앱 재기동, 미확정 앱의 고장 후 자동 롤백을 검증했다.
  flash 작업 중 센서의 연속 유효성은 별도이며 OTA의 기존 재부팅·건강확인 정책을 유지한다.
  인증된 `/status`의 `loop_watchdog_armed`, `loop_watchdog_deadline_ms`,
  `watchdog_fault_probe`로 활성 감시와 시험 코드 포함 여부를 확인할 수 있다.

최종 정지본 `wdt-20260913-final` 설치 및 자동 롤백 시험 후 120초 관측에서
STOP ACK 821/821, 텔레메트리 1199건/9.99508Hz/순번누락 0, 최대 수신간격 187ms,
추가 부팅 0회였다. 송신중단 구간의 link_ok=false 124건과 안전잠금 유지를 확인했다.
종료 시 HTTPS에서 감시 활성·750ms·고장주입 OFF·정상확정·구동 OFF를 확인했다.
이 수치는 정지 벤치 결과이며 보행·장시간·센서 정확도나 다른 기체의 보증은 아니다.

진행 순서: PC 상태/오류회귀 시험 → 기본/감시ON/센서ON/probe 빌드 → 장비 단독 소유
및 구동OFF/백업 확인 → 정상 기동·Wi-Fi 재접속·센서 부하 → probe의 명시 H 입력에
대한 리셋 시간 → 실제 앱 통합. probe는 UART H 전까지 고장을 주입하지 않는다.
호스트 모의 시계 시험은 실물 측정으로 기록하지 않는다.

전체 앱의 벤치 고장주입은 `MECHADOG_WATCHDOG_FAULT_PROBE=1`을 별도로 지정해야 한다.
기본 0이며 WDT OFF이면 빌드를 거부한다. 구동/OTA 동시 사용 금지도 그대로 적용된다.
이 빌드에서만 UART H를 수신하면 마지막 진척 갱신 후 의도적 무한루프에 들어간다.
실물 확인 전 H를 보내지 않는다. 정상/출시 이미지에는 이 경로를 넣지 않는다.

**OTA OFF 빌드에서는 자동 OTA 롤백을 복구 전제로 삼지 않는다.** SDK 헤더의 롤백 설정과
설치된 부트로더의 실제 동작은 별개다. OTA OFF 빌드에는 승인 지연 override가 없어
Arduino 기본 `verifyRollbackLater=false`, `verifyOta=true`가 앱을 조기 VALID로 확정하므로,
NEW 상태 시험 슬롯을 선택해도 리셋 뒤 같은 시험 앱이 부팅한다(2026-09-13 실기 — 실제 selector의
seq7/state2와 설치 ELF의 두 기본 함수 반환값으로 확인). 복구는 기존 정상 슬롯을 쓰지 않고 보존한 채
원래 부팅 선택 영역(4KiB)을 명시적으로 되돌리고, 기존 정상 OTA 슬롯의 이미지 일치와
HTTPS healthy/confirmed/구동OFF를 확인한다. 정상 OTA 빌드의 승인 지연/확정 경로와는 구분한다.

근거: [ESP-IDF FreeRTOS 태스크 API](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/freertos.html),
[시스템 재시작 API](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/system.html).

## SERVICE 모드 — 구동 빌드 런타임 워치독 (2026-09-15)

워치독을 별도 구동OFF 펌웨어가 아니라 구동 펌웨어의 확장팩으로 둔다.
`MECHADOG_ENABLE_TASK_WDT=1`과 `MECHADOG_ENABLE_ACTUATORS=1`을 함께 켜면
`MECHADOG_SERVICE_MODE=1`이 도출되고 부팅 시 워치독은 해제 상태다.
벤더 시작 코드가 리셋 직후 몸을 움직일 수 있으므로, 워치독 재부팅이 이미 정지한
상태에서만 일어나게 하기 위해서다.

- **진입**: `SERVICE mode=enter` 명령 또는 캐리어보드 사용자 버튼(GPIO5,
  벤더 `Key_Pin`, 액티브 로우 풀업, 50ms 디바운스, 눌림 에지 토글) —
  몸 정지 → SAFE 래치 → MOVE/POSE 차단 → 데드라인 arm. 워치독 재부팅은
  구조적으로 정지 상태에서만 발사된다(벤더 초기화 거동 = 일반 부팅과 동일).
- **해제**: `SERVICE mode=exit` 또는 버튼 재입력 — disarm 먼저, SAFE 래치는
  유지된다. 보행 복귀에는 별도 `RESET_SAFE`가 필요하다.
- **의도적 행위**이므로 `failsafe_count`를 올리지 않는다. 상태는 ACK의
  `service_mode`·`wdt_armed`와 텔레메트리 `flags.service`로 확인한다.
- 서비스 모드 중 정지 OTA(`/firmware`·`/confirm`)를 받는다 — 주차 상태가
  아니면 `not_parked`(409)로 거절한다.
- 구동OFF 진단 빌드는 부팅 시 arm 된다 — SERVICE 모드는 없다.

자세한 명령 규약은 [PROTOCOL.md](../../docs/PROTOCOL.md)의 `SERVICE` 항을 본다.

확장팩 빌드(실물 설치 명령 아님, private_ota.h 경로는 각자의 것):

```bash
arduino-cli compile --fqbn esp32:esp32:esp32:FlashMode=dio,FlashFreq=40 \
  --build-property 'compiler.cpp.extra_flags=-DMECHADOG_ENABLE_SENSORS=1 -DMECHADOG_ENABLE_ACTUATORS=1 -DMECHADOG_ENABLE_TASK_WDT=1 -DMECHADOG_ENABLE_OTA=1 -DMECHADOG_OTA_VERSION=\"svc-dev\" -include /path/to/private_ota.h' \
  --build-path build/svc-motion firmware/mechdog_motion
```

**실기 확인 (2026-09-23 · `mechdog-02`)** — 보드 사용자 버튼(GPIO5) 눌림으로
`service_mode=true` 와 `loop_watchdog_armed=true`(750ms 마감)를 텔레메트리·OTA status 로
확인했다. SERVICE 명령 왕복은 약 0.1s 다.

저장소 루트에서 구동 OFF 감시 빌드를 만든다(실물 설치 명령 아님).

```bash
arduino-cli compile --fqbn esp32:esp32:esp32:FlashMode=dio,FlashFreq=40 \
  --build-property 'compiler.cpp.extra_flags=-DMECHADOG_ENABLE_TASK_WDT=1 -DMECHADOG_ENABLE_ACTUATORS=0' \
  --build-path build/wdt-motion firmware/mechdog_motion
```

독립 시험 스케치 `diagnostics/watchdog_probe`에는 모터·I2C·Wi-Fi·벤더 라이브러리가 없다.
기동 후 UART로 대문자 `H`를 받을 때만 마지막 갱신 뒤 의도적으로 무한루프에 들어간다.
리셋 뒤에는 새 `H`를 받아야 다시 주입되므로 자동 반복 리셋을 만들지 않는다.
공용 워치독 헤더를 재사용하기 위해 `src`의 **절대 경로**를 include 경로로 지정한다.

```bash
arduino-cli compile --fqbn esp32:esp32:esp32:FlashMode=dio,FlashFreq=40 \
  --build-property 'compiler.cpp.extra_flags=-I/absolute/path/to/mechadog/firmware/mechdog_motion/src' \
  --build-path build/wdt-probe firmware/mechdog_motion/diagnostics/watchdog_probe
```

시험 스케치를 본체에 설치하면 기존 텔레메트리 앱을 대체한다. 설치 전 앱/복원 파일을
확정하고 기존 개체별 보호 영역 검증 절차를 적용해야 한다. 이 문서의 빌드는 설치가 아니다.
실기 검수는 ① 정상 부하에서 불필요한 리셋 없음 ② `H` 주입 후 새 boot/reset reason
③ 고장 시점부터 리셋까지 1초 이내의 계측 근거 ④ 본체 후보의 SAFE 유지 순서로 진행한다.
UART 호스트 도착 간격에는 버퍼/부팅 시간이 섞이므로 정밀 리셋 시각으로 취급하지 않는다.
JTAG/OpenOCD 디버깅이 워치독을 비활성화할 수 있어 그 조건도 기록해야 한다.

CI는 구동 OFF 감시 빌드(기본·센서 ON·고장주입 ON)와 독립 시험 스케치, 정지 OTA 감시 빌드를
컴파일한다(릴리스 산출물에는 넣지 않는다). 벤더 라이브러리가 CI에 없어 구동 빌드는 컴파일할 수
없으므로, 구동 빌드가 SERVICE 모드로만 감시를 켠다는 계약은 소스 수준 검사로 지킨다.
감시 태스크의 상태·오류 경로는 `test/test_task_watchdog.cpp`가 PC에서 시험한다.
실제 하드웨어 타이머 동작은 PC 시험으로 증명할 수 없어 위 실기 측정이 근거다.

## 4.1.4 현재 상태 — 정지 수신 및 재부팅 2회 통과 (2026-09-12)

부팅마다 새 `boot_id`를 만들고 `device_id`·`boot_id`·`seq`를 포함한 텔레메트리를
10Hz로 PC의 5101 포트에 보낸다. 구성 요소는 다음과 같다.

- `src/telemetry_encoder.*`: 규약 검사, JSON 직렬화, 개체·부팅별 송신 순번.
- `src/telemetry_publisher.*`: 별도 UDP 소켓으로 PC의 5101 포트에 보내는 주기 송신 구성 요소.
- `src/sensor_hal.*`: 본체 센서 취득, 유효성·신선도와 오류 상태를 제공하는 HAL.
- [`tools/probe/telemetry_probe.py`](../../tools/probe/telemetry_probe.py): PC에서 수신 기록과 주기를 확인하는 도구.

센서 취득과 주기 송신 호출은 `mechdog_motion.ino`에 연결돼 있다.
유효 명령 수신 시 PC 주소·epoch 시각을 설정하고 watchdog 처리 뒤 센서 스냅샷을
송신한다. `PING`·폐기 패킷은 시각/목적지/명령 나이를 갱신하지 않는다.

Wi-Fi 재시도는 3초 간격이다. 이미 AP에 연결돼 있으면 DHCP를 기다리고, 미연결이면
`esp_wifi_connect()`로 접속을 요청한다. `WiFi.reconnect()`는 Arduino-ESP32 2.0.12에서 먼저
`esp_wifi_disconnect()`를 불러 접속·DHCP를 끊을 수 있으므로 쓰지 않는다. SDK 자동 재접속·NVS
저장 정책은 그대로 둔다. Wi-Fi 이벤트 사유/콜백 도착 시각을 고정 길이 큐로 전달하고,
재시도 반환값·호출 소요 시간을 진단한다. 콜백은 로그 출력이나 재접속을 수행하지 않으며
큐 초과 횟수를 기록한다. SDK 내부 재접속 처리 이후 도착한 콜백 시각을 실제 무선 이벤트
발생 시각으로 해석하지 않는다. 부팅 시 Wi-Fi 접속을 기다리며 멈추지 않고 재접속은 루프에서 처리한다.

2026-09-12 센서 ON·구동 OFF 빌드(retry1)에서 같은 수신 소켓과 decoder를 유지하며
세 구간을 각각 35초씩 측정했다.

| 구간 | 수신 건수 | 관측 Hz | 첫 seq | seq 누락 | 최대 간격 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 초기 부팅 | 351 | 10.00915 | 1 | 2 | 203ms |
| EN 재부팅 1 | 352 | 10.04637 | 1 | 1 | 156ms |
| EN 재부팅 2 | 352 | 10.03746 | 1 | 1 | 156ms |

총 1,055건을 실제 규약 decoder가 수락했고 새 boot의 seq1 수락을 2회 확인했다.
각 구간의 주기/연속성 기준을 통과했다. 리셋 경계의 이전 boot 패킷과 무관한 ACK는
별도로 구분한다. 의도한 재부팅·접속 대기를 포함한 전체 capture 연속성은 fail로 남으며,
이 공백을 지우거나 무손실 송신으로 보고하지 않는다.
UART에서 정상 IMU 상태 166줄을 확인했고 timing fault는 없었다.
AP 접속 이벤트는 약 1.7초, GOT_IP 이벤트는 부팅별 11.4/6.4/34.4초에 도착했다.
명령상 정지 상태에서도 부팅별 yaw 값 변화가 관측됐다. 자이로 시작 보정/드리프트와
축·정확도는 검증하지 않았으므로 절대 방위나 정밀 자세로 쓰지 않는다.
보행 중 10Hz 는 아래 *"구동·센서 통합 빌드 준비"* 6단계의 공중 보행 실기가 근거다.

### 센서 HAL 진단

배터리 값은 ADC raw 9회의 중앙값을 `esp_adc_cal_raw_to_voltage()`로 바꾼 뒤 공식 회로의
분압비 **4**를 곱한다. eFuse two-point/Vref 보정을 우선 사용하고, 없으면 기본 Vref로 변환하며
그 출처를 진단에 표시한다. 외부 계측기로 전체 경로를 검증한 `battery_calibrated` 상태는
false다. ADC 보정은 충전 전압을 제어하는 기능이 아니다.

UART 진단 출력은 출력 공간에 **128 bytes 추가 여유**가 있을 때만 쓰고, 진단 출력 누락 횟수를
기록하며 출력하지 못한 구간의 최대 루프 간격도 보존한다. 이때 생략하는 것은 진단 로그이며
센서 샘플이나 텔레메트리가 아니다.

최초 IMU timing fault의 조건과 발생 시각, 예정 기상 대비 지연, 해당 회차의 경과 시간 및
IMU/필터·초음파·ADC 단계의 경과 시간을 보존한다.
`imu_timing`은 `wake_late`, `cycle_elapsed`, `scheduled_elapsed`를 구분하며
`imu_stage_mask`의 비트 1/2/4는 각각 IMU·필터/초음파/ADC 시간의 측정 여부다.
mask에서 빠진 단계의 0은 측정 결과가 아니다. 단계 시간에는 선점·인터럽트·드라이버 대기가
포함되므로 특정 부품의 고장이나 CPU 사용 시간으로 단정하지 않는다. 이 기록은 최초 오류
시점의 값으로 이후 정상 회차가 덮어쓰지 않는다.

기본값은 `MECHADOG_ENABLE_SENSORS=0`, `MECHADOG_ENABLE_ACTUATORS=0`이며 두 기능을 함께 켤 수
있다 — I²C 0번 포트는 센서 HAL이 쥐고 벤더의 I²C 기능은 부르지 않는다(아래 *"구동·센서 통합
빌드 준비"* 5단계). 센서 측정 유효 플래그와 본체 축 검증·배터리 보정 완료 여부는 서로 다르다.
yaw는 필터 기준 상대 자세이며 나침반의 절대 방위가 아니다.
필터 시작 시 정지 상태의 자이로 오프셋을 취득하며, 시작 yaw가 0도라는 보장은 없다.
필터 시작 후 취득 실패나 전체 주기 누락이 발생하면 HAL은 IMU를 재부팅까지 무효화한다.

센서값과 실제 안전 로직 발동 상태를 구분한다. `safety_latched`는 기존 실제 래치,
`state`는 래치 중 `FAILSAFE`, 그 외 수신 `STATE` 반향이다. `RESET_SAFE` 성공 시
`IDLE`로 복귀한다. 배터리 경고는 정본의 7.0V 이하 조건이다.
`link_ok`는 유효 명령을 받고 3초 이내인 상태로, 600ms 보행 watchdog과 구분한다.
전도 자동 정지는 폐기됐으므로(ADR-36) `tipped=false`는 정상 자세를 보증하지 않는다.
선택 필드 `obstacle`은 아래 근거리 반사 정지가 걸려 있는지를 싣는다.
publisher의 시각은 수락한 명령의 epoch를 수신 시각에 맞춰 추정하므로 명령 전송 지연이
포함된다.

⚠️ **충전기는 공식 8.4V 충전기만 쓴다**([공식 충전 안내](https://wiki.hiwonder.com/projects/MechDog/en/latest/docs/1.Getting_Ready.html)).
출력이 다른 충전기(예: 12.6V)가 물려 있으면 업로드·통신·시험을 멈춘다. 충전기 라벨이나
미보정 ADC 값만으로 실제 배터리 전압, 손상 여부 또는 기체의 사용 가능 상태를 판정하지 않는다.

⚠️ **앱 영역은 무압축으로만 기록하고 보호 영역을 대조한다.** ROM 압축 기록은 보호 영역
0x6000~0x8fff를 바꾼 사례가 있고(2026-09-11 — 원본 세 섹터를 무압축 복원한 뒤 대조 통과),
사용한 도구에서는 `compress=False`만 전달해도 stub의 기본 압축이 해제되지 않았다.
NVS·순정 VFS를 보존하므로 기본 Arduino 파티션이나 boot_app0를 포함한 일반 업로드를 적용하지 않는다.

## 안전 동작

- 부팅 직후에는 SAFE 잠금 상태이며 `MOVE`를 실행하지 않는다.
- 복구할 때는 `STOP` → `RESET_SAFE` 순서로 보내고, 그 뒤 새 `MOVE`부터 실행한다.
- ⚠️ **원인이 남아 있으면 `RESET_SAFE` 를 거부한다** (`3.2.5`) — 저전압으로 세운 기체는 전압이 **경고선(7.0V) 위로 회복될 때까지** 풀리지 않는다. 셧다운선 바로 위에서 풀어 주면 다음 보행 부하에 다시 걸려 사람이 해제만 반복하게 된다. 근거리 정지는 래치 원인이 아니라 해제를 막지 않는다. 전도 자동 감지는 폐기돼(`ADR-36`) 이 판정에 없다
- `ESTOP`은 즉시 정지하고 SAFE를 잠근다.
- 정상적으로 파싱된 명령이 600ms 동안 없으면 정지하고 SAFE를 잠근다.
- 기형 JSON, 모르는 타입과 중복·역전 `seq`는 워치독 시간을 갱신하지 않는다.
- Wi-Fi가 끊기면 즉시 정지하고 SAFE를 잠근다.

`PING ...`에는 `ACK PING ...`으로 응답하지만 진단 패킷이므로 워치독 시간을
갱신하지 않는다.

### 온보드 판정 — `src/safety_monitor.*` (WBS 3.2.2 · 3.2.6)

센서 값만 받아 판정하는 하드웨어 무관 코드다. 시험은 `test/test_safety_monitor.cpp`
가 PC 에서 돌린다(CI 포함).

| 판정 | 규칙 |
| :--- | :--- |
| 근거리 반사 정지 | `7cm` 미만([ADR-47](../../docs/DECISIONS.md#adr-47))이 **연속 2표본**(센서 40ms 주기 → 약 80ms)이면 즉시 `move(0,0)`. 따라가기 주행은 LiDAR 정면 0.25m 에서 먼저 서므로 초음파는 LiDAR 높이 아래 낮은 물체용 마지막 안전장치다 |
| 그동안 거부하는 것 | ⚠️ **전진 `MOVE` 만.** 후진·선회·정지는 통과시킨다 — 초음파는 정면만 보므로 물러나는 것이 유일한 탈출로다 (FR-2.3) |
| 해제 | `10cm` 이상이 **연속 5표본(200ms)**. 걸 때보다 멀리서·오래 본다 — 초음파는 표적 앞에서 먼 값을 섞어 읽는다. 21cm 표적 앞에서 13% 꼴로 34cm 를 섞어 읽어(2026-09-17 실기) 2표본 해제면 차단이 12초에 29번 깜빡였다 |
| 보고 | 걸려 있는 동안 `state=AVOID` 와 `flags.obstacle=true`. 호스트는 **플래그로** 해제를 안다 (ADR-22) |
| 저전압 경고 | `7.0V` 이하면 `flags.lowbatt` (표시만, 동작을 막지 않는다) |
| 저전압 셧다운 | `6.6V` 이하가 **연속 3표본**이면 SAFE 래치. 자동 복귀 없음 — `RESET_SAFE` 가 필요하다 |
| 값이 없거나 오래되면 | 새로 걸지도, 걸린 것을 풀지도 않는다. **없는 값으로 판정하지 않는다** |
| 우선순위 (`3.2.5`) | **E-Stop · 타임아웃 · 링크 두절 · 저전압 > 근거리 정지 > 호스트 명령.** 조합 판정은 `move_allowed()`·`reset_safe_allowed()` **한 곳**에 있고 스케치는 그것만 부른다 |

⚠️ **`MECHADOG_BENCH_BATTERY_THRESHOLDS=1` 은 벤치 전용이다.** 가변전원이 없을 때
만충 근처에서 교차를 만들려고 임계를 `8.2V / 8.0V` 로 올린다. 판정 코드는 정식
빌드와 같은 경로이며, **확인이 끝나면 끄고 다시 올린다.**

## 기본 빌드

저장소의 기본값은 `MECHADOG_ENABLE_ACTUATORS=0`이다. 이 빌드는 네트워크,
파서와 안전 상태만 검증하고 서보를 초기화하지 않는다.

MechDog의 로컬 검증과 CI 빌드는 **ESP32 Arduino core 2.0.12 / ESP-IDF 4.4.5**를
기준으로 하며, 빌드 재현성을 위해 core 버전과 DIO·40MHz 설정을 고정한다.
ESP32 보드 매니저 인덱스를 등록한 환경에서 같은 버전을 설치한다.

```powershell
arduino-cli core install esp32:esp32@2.0.12
arduino-cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" firmware/mechdog_motion
```

위 `compile` 명령은 DIO·40MHz로 컴파일만 하며 업로드를 수행하지 않는다.
CI의 XIAO-Vision·Lidar-Relay 타깃도 같은 core 2.0.12를 쓴다.
기본 Wi-Fi 접속 경로는 ESP32 NVS에 이미 저장된 값을 `WiFi.begin()`으로 재사용한다.
NVS에 유효한 STA 접속 정보가 없는 최초 설치에는 아래 선택 빌드 설정을 사용한다.

### 최초 Wi-Fi 연결을 위한 선택 빌드 설정

다음 두 매크로를 저장소 밖의 접근 제한된 개인 헤더에 C/C++ 문자열 값으로 정의한다.
빌드 시 해당 헤더를 `-include`로 포함하며, 실제 값을 명령행에 직접 나열하거나
저장소 소스·문서·로그 출력에 넣지 않는다.

- `MECHADOG_WIFI_SSID`
- `MECHADOG_WIFI_PASSWORD`

두 매크로를 **모두 정의하면** `WiFi.begin(ssid, password)`로 접속한다.
**둘 다 정의하지 않으면** 기존 `WiFi.begin()`의 저장된 NVS 설정 사용을 유지한다.
**하나만 정의하면** 컴파일 오류로 중단한다. 매크로 가드는 두 설정의 존재를 검사하며,
실제 접속 정보의 유효성이나 Wi-Fi 연결 성공을 보증하지 않는다.

이 설정은 최초 Wi-Fi 연결을 위한 빌드 설정이다. UDP 명령·텔레메트리 경로와 SAFE 래치는
그대로 유지한다. 컴파일된 이미지에도 접속 정보가 포함되므로 개인 헤더와 해당 빌드의
생성 소스·BIN·ELF·로그는 접근 제한된 개인 폴더에 보관하고 저장소에 추가하지 않는다.
기본 빌드에는 이 개인 헤더가 필요하지 않다.

## 센서 진단 빌드의 외부 의존성

센서를 켜는 빌드는 다음 라이브러리가 외부 Arduino 라이브러리 경로에 있어야 한다.

- **SensorLib v0.2.1**: `SensorQMI8658.hpp`를 제공한다. 현재 구현은 이 버전의 API를 기준으로 한다.
- **Madgwick 1.2.0**: `MadgwickAHRS.h`를 제공한다. Arduino 라이브러리 관리자에서 받으며, 벤더
  예제 자료의 사본은 쓰지 않는다. 센서 빌드는 이것으로 `--warnings all` 컴파일된다.

설치 명령은 아래 *"구동·센서 통합 빌드 준비"* 2단계에 있다.
CI의 MechDog 릴리스 빌드는 **센서 OFF / actuator OFF 기본 빌드**다. 센서 ON 경로는
감시 빌드 단계에서 SensorLib와 Madgwick을 설치해 구동 OFF로 컴파일만 확인한다.
따라서 CI 성공은 실제 센서 취득, 10Hz 송신이나 구동 공존을 검증한 결과가 아니다.
센서 ON 빌드는 위의 core 버전과 아래 명령으로 로컬에서도 확인할 수 있다.

이 외부 소스를 저장소에 자동으로 복사하거나 임의의 다른 버전으로 바꾸지 않는다.
라이브러리 탐색과 빌드 구성을 먼저 확인한 뒤, 센서 진단 설정으로 컴파일한다.

```powershell
arduino-cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" --build-property "compiler.cpp.extra_flags=-DMECHADOG_ENABLE_SENSORS=1 -DMECHADOG_ENABLE_ACTUATORS=0" firmware/mechdog_motion
```

센서 ON 설정은 연결된 초기화·취득 경로를 켠다. 부팅 후 정지 상태의 오프셋 취득과
모든 센서의 유효값이 필요하며, 오류 중에는 정상 텔레메트리를 만들지 않는다.
Wi-Fi는 위에서 선택한 설정으로 접속하고 재접속은 루프에서 처리한다. 부팅 시 Wi-Fi 접속을 기다리지 않는다.

## 구동·센서 통합 빌드 준비 — 벤더 파일·라이브러리·보드 패키지

**저장소에 없는 파일이 필요하다.** Hiwonder 예제 소스에 라이선스 표기가 없어 재배포할 수
없기 때문이다 ([ADR-20](../../docs/DECISIONS.md)). 각자 받아 두고, **빌드 전에 점검 도구로 확인한다.**

> **이 절차로 센서+구동 통합 빌드가 컴파일된다** (2026-09-12 확인 · core 2.0.12 ·
> flash 800,921 B (61%) · 정적 RAM 49,092 B). 공중 보행 중 텔레메트리 10Hz 실기 결과는
> 아래 6단계에 있다(mechdog-01).

### 0단계 — 점검부터 한다

```powershell
python tools/dev/firmware_env.py
```

벤더 파일 11개(크기·SHA-256) · 벤더 파일이 git 에서 무시되는가 · 라이브러리 4개의 버전 ·
보드 패키지 2.0.12 를 본다. **점검만 하며 받지도 설치하지도 않는다.** 전부 `OK` 가 아니면
아래 순서로 채운 뒤 다시 돌린다. 격리 폴더를 다른 곳에 두었으면 `--arduino-dir` 로 알려 준다.

### 1단계 — 도구: Arduino IDE 2 에 들어 있는 `arduino-cli`

따로 설치할 필요가 없다. Arduino IDE 2 설치본 안에 있다.

```powershell
$cli = "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
```

### 2단계 — 격리 폴더에 보드 패키지와 센서 라이브러리 설치

MechDog 는 core **2.0.12** 로 고정한다. 쓰던 IDE 설정의 core 버전과 섞이지 않게
**MechDog 전용 폴더**(`%USERPROFILE%\.arduino-mechdog`)를 쓴다 — 쓰던 IDE
설정은 건드리지 않는다. 저장소 밖이므로 git 과도 무관하다.

```powershell
$env:ARDUINO_DIRECTORIES_DATA = "$HOME\.arduino-mechdog\data"
$env:ARDUINO_DIRECTORIES_DOWNLOADS = "$HOME\.arduino-mechdog\staging"
$env:ARDUINO_DIRECTORIES_USER = "$HOME\.arduino-mechdog\user"
$env:ARDUINO_BOARD_MANAGER_ADDITIONAL_URLS = "https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json"
& $cli core update-index
& $cli core install esp32:esp32@2.0.12
& $cli lib update-index
& $cli lib install SensorLib@0.2.1 Madgwick@1.2.0
```

환경 변수는 **그 PowerShell 창에서만** 유효하므로 컴파일도 같은 창에서 한다. 용량은 받는
캐시 약 0.85GB(`staging` — 설치 뒤 지워도 된다)와 설치본 약 2.8GB 다. SensorLib 은 **0.2.1
이어야 한다** — 센서 HAL 이 이 버전 API 에 맞춰져 있다(최신 0.4.x 가 아니다).

### 3단계 — 벤더 파일: 받는 곳과 둘 곳

**받는 곳** — Hiwonder 의 `Arduino Programming Projects.zip`
([Hiwonder Wiki · Resources Download](https://wiki.hiwonder.com/projects/MechDog/en/latest/docs/8.Resources_Download.html)).
설치 프로그램 `MechDog V1.3.exe` 와 같은 배포물이 아니다(2026-09-11 확인).

**① 벤더 소스 11개 → 스케치 루트** (`src/` 가 아니다)

```
firmware/mechdog_motion/
├── mechdog_motion.ino
├── HW_MechDog.cpp   HW_MechDog.h      ← 벤더 (git 무시)
├── Hiwonder.cpp     Hiwonder.h
├── Servo.cpp        Servo.h
├── WMMatrixLed.cpp  WMMatrixLed.h
├── pwm_servo.cpp    pwm_servo.h
├── action.h
└── src/                               ← 우리 파일
```

`src/motion_hal.cpp` 는 `"../HW_MechDog.h"` 로 부른다. `src/` 의 파일에서는 스케치 루트가
포함 경로에 없어서 `"HW_MechDog.h"` 로는 **파일이 있어도 못 찾는다**
(2026-09-12 확인). 헤더를 `src/` 에 한 벌 더 복사하는 우회는 필요 없다.

**② 벤더 라이브러리 2개 → `%USERPROFILE%\.arduino-mechdog\user\libraries\`**

```
libraries/
├── MechDog_Arduino/   name=mechdog 1.2.5 — quad_kinematics.h · mech_base_types.h · 미리 컴파일된 .a
└── MPU6050/           1.3.1 (ElectronicCats · MIT)
```

`HW_MechDog.h` 가 이 둘을 다시 부르므로 ①만으로는 빌드되지 않는다.

> ⚠️ **`.gitignore` 가 벤더 파일 11개의 이름을 막는다** (루트와 `src/` 모두). 복사한 뒤
> `git status` 에 하나라도 보이면 **그 자리에서 멈춘다** — 공개 저장소에 올라가면 라이선스
> 위반이다. 점검 도구의 `git 무시` 항목이 같은 것을 본다.
>
> 해시의 기준은 `tools/dev/firmware_env.py` 의 `VENDOR_FILES` 다(2026-09-12 통합 빌드를 통과한
> 조합). 다른 배포본이면 해시가 다르게 나오는데, **틀렸다는 뜻이 아니라 검증하지 않은 조합**이라는
> 뜻이다 — 통합 빌드와 실기 확인을 다시 거친 뒤 표를 갱신한다.

### 4단계 — 컴파일

```powershell
# 센서 + 구동 (통합). 구동만이면 SENSORS=0, 센서만이면 ACTUATORS=0
& $cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" --build-property "compiler.cpp.extra_flags=-DMECHADOG_ENABLE_SENSORS=1 -DMECHADOG_ENABLE_ACTUATORS=1" firmware/mechdog_motion
```

⚠️ **벤더 파일이 스케치 폴더에 있으면 `--warnings all` 을 쓰지 않는다.** 벤더 코드의 경고
(반환값 누락 · 괄호 · 미사용 변수)가 오류로 바뀌어 **구동을 끈 빌드까지** 실패한다 — 스케치
루트의 `.cpp` 는 설정과 무관하게 항상 함께 컴파일되기 때문이다. CI 와 같은 조건으로 **우리
코드만** 확인하려면 벤더 파일이 없는 사본에서 컴파일한다.

```bash
git ls-files -z firmware/mechdog_motion | xargs -0 cp --parents -t <빈 폴더>
```

### 5단계 — I²C 규칙: 벤더의 I²C 기능은 부르지 않는다

센서 HAL 이 **I²C 0번 포트(SDA22/SCL23)** 를 쥔다. 벤더도 같은 포트·핀을 `IIC1` 로 감싸지만
그것을 여는 곳은 `homeostasis()`(자세 균형)·`UltrasoundSonar`·`MP3Sensor` 뿐이고, 우리가 쓰는
`MechDog_init()`·`move()` 는 I²C 를 쓰지 않는다(벤더 소스 2024-08 판과 미리 컴파일된 라이브러리의
기호로 확인). 그래서 **그 셋을 우리 코드에서 부르지 않는 것**이 통합의 조건이며
`tests/test_firmware_vendor_boundary.py` 가 막는다. 새 I²C 장치도(MP3 모듈 등)
벤더 클래스가 아니라 센서 HAL 의 버스를 통한다.

#### 같은 버스의 음성 모듈과 MP3 모듈 — 2026-09-23 실측

로봇 4핀 포트 가운데 이 버스에 닿는 것은 **IIC1 하나뿐**이다(다른 포트에 꽂으면 `0x34` 가
보이지 않았다). IMU(`0x6A`)·초음파(`0x77`)와 함께 쓰는 안전 버스다.

| 장치 | 주소 | 오가는 것 |
| :--- | :--- | :--- |
| WonderEcho CI1302 음성 모듈 | `0x34` | **명령 id 만.** `0x6E ← [type, id]` 2바이트로 모듈 매핑표의 고정 문구(영어)를 재생하고, 레지스터 `0x64` 에서 모듈이 인식한 명령 id 를 읽는다(한 번 읽으면 지워진다). `0x70` 쓰기는 무시된다. 음성 파형(PCM)은 어느 방향으로도 오가지 않는다 |
| MP3 모듈 | `0x7B` (`0x7E` 도 보임 · 정체 미확인) | 레지스터 1=곡 번호, 5=재생, 6=일시정지, 9=다음 곡, `0x0C`=음량. **2026-09-24 실측:** `0x01 ← 번호`(2바이트 리틀엔디언) 하나로 재생이 시작되고 번호는 파일 이름 앞 네 자리다. `0x05` 는 재생이 시작됐고(모듈 전원 직후 첫 곡 · 멈춘 곡을 이어 트는지는 재지 않았다) `0x06` 은 멈춘다. 음량은 재생 중에 보낸 것만 먹는다 |

⚠️ **`0x64` 는 장치 주소가 아니라 `0x34` 모듈 안의 레지스터다.** 모듈의 I²C 주소는 `0x34` 하나다.

⚠️ **`0x7B` 는 I²C 예약 구간(`0x78`–`0x7F`)에 있다.** 스캔을 `0x77` 까지만 하면 보이지 않는다.

그래서 로봇 경유 음성 중계(WBS 4.7.9)는 불가하고, 말하기는 MP3 모듈이 TF 카드에
미리 넣어 둔 문장 트랙을 재생한다([ADR-38](../../docs/DECISIONS.md#adr-38),
[측정](../../docs/measurements/2026-09-23-bridge-transparency.md)). 벤더 SDK 의 `MP3Sensor` 는
쓰지 않는다 — 위 규칙대로 부르지 않고, 예제 코드도 저장소에 넣지 않는다([ADR-20](../../docs/DECISIONS.md)).
MP3 드라이버는 **자체 코드로 센서 HAL 의 버스를 거친다**(WBS 4.7.20 · 2026-09-24 **실기 확인**).
센서 HAL 의 `writeMp3Volume`·`writeMp3Track` 이 I²C 버스 잠금을 지켜 쓰고, `src/mp3_player.h` 가 «언제
무엇을 쓰는가» 를 정한다. `SOUND` 의 인자는 **`track`**(0\~3000, 0 = 정지)이다 —
동작 규칙은 [PROTOCOL](../../docs/PROTOCOL.md) 의 `SOUND` 절이 정본이다.

- 재생 때마다 트랙 번호를 레지스터 `0x01` 에 2바이트(하위 먼저)로 쓰고, 20ms 뒤 음량 레지스터(`0x0C`)에
  `MECHADOG_MP3_VOLUME`(기본 28 — 2026-10-06 시연 청취 기준, 0\~30, 빌드 플래그)을 쓴다. 트랙 쓰기 하나가 곧 재생 시작이다.
  ⚠️ 음량을 트랙보다 먼저 쓰면 안 된다 — 모듈이 재생 전에 받은 음량을 버려 최대 크기로 나온다(실측).
  `0` 은 일시정지 레지스터(`0x06`)로 보내고 음량은 쓰지 않는다.
- 20ms 간격은 `delay()` 가 아니라 루프 회전으로 지킨다. 모듈이 응답하지 않으면 5회 시도 뒤 버리고 시리얼에
  `SOUND dropped` 를 남긴다.
- 안전 래치 중에도 받는다(눈 LED 와 같다). 범위 밖 트랙과 센서를 끈 빌드는 `applied=false` 다.

**실기 확인 (2026-09-24 · `mechdog-01`).** 안전 래치 상태에서 `SOUND 17` 이 `0017江南Style` 을 음량 15 로
틀었고, `SOUND 0` 이 멈췄고, `SOUND 3001` 은 `applied=false` 였다. 트랙 번호는 **파일 이름 앞 네 자리**다
(카드에 없는 `2` 는 무음). 원자료 `field_tests/results/20260924_4.7.20-mp3-driver/`. `0x7E` 의 정체는 확인하지 않았다.

⚠️ **MP3 모듈은 IIC1 포트에만 꽂는다.** 다른 4핀 포트에 꽂자 모듈은 I²C 에 응답하지 않았고, 그동안
배터리 ADC(GPIO34)가 **4095 에 붙었다**(빼자 곧 정상). 그 포트는 배터리 측정 핀과 이어져 있다.

#### 진단 스케치 — `diagnostics/i2c_bridge_probe/`

위 표를 만든 USB 시리얼 시험 스케치다. 모터·Wi-Fi·벤더 라이브러리가 없다. 115200 baud 줄 단위
명령은 `scan`(IIC1·IIC2, `0x08`–`0x7F`)·`r`·`drain`·`w`·`map`·`burst`·`unstick` 이고 자세한 인자는
스케치 머리 주석에 있다. 쓰기(`w`·`burst`)는 `0x34`·`0x7B` 에만 허용한다 — 같은 버스의 IMU·초음파를
오타로 건드리지 않기 위해서다. ⚠️ Arduino-ESP32 는 반복 시작 읽기의 `endTransmission(false)` 를
`requestFrom` 까지 미루므로 `tx` 는 늘 0 으로 찍힌다. 판정은 `got` 으로 한다.

⚠️ **올리면 운영 펌웨어가 덮어써진다.** 서보에 명령이 가지 않으므로 서 있던 로봇은 주저앉는다.
머리 주석대로 배터리를 빼고 USB 전원만으로 쓴다(4핀 5V 는 USB 전원으로 살아 있다). 설치는 아래
6단계의 2026-09-12 기록과 같은 **앱 단독 기록**으로 한다.

1. 파티션 표를 읽고 기존 `app0` 을 개발 PC 에 백업해 해시를 적어 둔다.
2. 스케치를 `esptool write_flash -u 0x10000` 으로 **`app0` 에만** 기록한다.
3. 시험이 끝나면 백업한 `app0` 을 같은 방식으로 되돌리고, 쓰기 해시가 백업과 같은지 확인한다.

2026-09-23 시험도 이 순서였다(백업 SHA256 `1084ab8d…`, 되돌린 뒤 쓰기 해시 일치).

벤더는 부팅 때 **배터리 감시 태스크**도 띄운다 — GPIO34 를 0.1초마다 읽고 `raw×3.6` 이 7.0V
미만이면 부저를 울린다. 우리 센서 HAL 도 같은 핀을 읽는다. ADC 접근은 드라이버가 순서를
맞추며 그 순서를 실기로 따로 확인하지는 않았다.

### 6단계 — 업로드와 첫 실기 확인

이 절은 **컴파일까지**다. 업로드는 아래 *"순정 기체에서 업로드하기 전"* 절을 따른다 — 일반
업로드는 NVS 의 보정값을 덮을 수 있다. ⚠️ **업로드하면 `MechDog_init()` 이 서보를 초기화한다.**
처음에는 몸통을 받쳐 **네 발을 띄운 상태**로 시험한다. 확인할 것:

- 걷는 중에도 텔레메트리가 10Hz 로 오는가 (`tools/probe/telemetry_probe.py`)
- UART 에 `imu_timing` 오류가 없는가 — 센서 태스크는 40ms 늦으면 **재부팅까지 IMU 를 끄고**,
  그러면 텔레메트리 전체가 멈춘다. 보행 계산과 같은 코어에서 돈다
- 배터리 값이 벤더 부저 경고와 어긋나지 않는가

#### 2026-09-12 실기 결과 — mechdog-01, 네 발을 띄운 공중 보행

설치 전에 기체의 파티션 표(`app0` = 0x10000, 1,310,720 B)를 읽고 이전 앱·NVS·`otadata` 를
개발 PC 에 백업한 뒤, `esptool write_flash -u 0x10000` 으로 **앱만** 기록했다(해시 검증 통과).
`otadata` 가 `app0` 을 가리키는 것도 확인했다. 백업은 저장소 밖에 둔다.

| 시험 | 결과 |
| :--- | :--- |
| 부팅 | `Actuators: ON` · `Sensors: enabled=1` · Wi-Fi 4.5초 · IMU·거리·배터리 유효 · `imu_timing=none` |
| 공중 보행 20초 (`mechdog_command.py move --step 60 --angle -8`) | `MOVE` 200/200 적용 · 텔레메트리 340/340 수락 · **9.998Hz** · seq 누락 0 · 최대 간격 0.11초 · `imu_timing=none` · 명령이 끊기자 워치독 래치 |
| `host.runtime --reset-on-start --patrol` 25초 | 로봇의 `latched=false` 보고로 리셋 정착 → **1초 안에 PATROL** → SCAN → PATROL → SCAN · 틱 간격 p95 110ms · 종료 `ESTOP` 래치 |

⚠️ **배터리 측정값은 흔들린다 — 그래서 중앙값을 싣고 폐기 상한을 8.6V 로 둔다.** 2026-09-12 정지 중
배터리 측정값이 7.72\~8.46V(중앙값 8.25V)로 흔들렸고, 1초 진단 34개 중 9개가 8.4V 를 넘었다.
인코더는 범위 밖 텔레메트리를 통째로 버리므로 상한이 8.4V 이면 SCAN 구간에서 수신이 초당
2\~6건까지 떨어진다(`battery outside [6.0,8.4]` 22초 기록). **완충 직후에는 대부분이 버려져
호스트가 링크 두절(3초)로 FAILSAFE 에 들어갈 수 있다.**

그래서 펌웨어는 ADC 를 9회 읽어 **중앙값**을 싣고, 폐기 상한은 **8.6V** 다 (만충 8.4V + 측정 여유
0.2V · [ADR-30](../../docs/DECISIONS.md)). 경고 7.0V·셧다운 6.6V 와는 별개다.

**확인 (2026-09-13 · mechdog-01 · 만충 직후)** — 폐기 0, 텔레메트리 **180/180 수락 · 10Hz · 최대 간격 0.11초**(`telemetry_probe` `rate_check: pass`). 측정 폭은 0.22V(중앙값 없이 0.74V)이고 값은 8.256\~8.472V(중앙 8.396)다. ⚠️ **만충에서도 15건 중 5건이 8.4V 를 넘는다** — 상한이 8.4V 이면 그 3분의 1이 버려진다.

⚠️ **충전기를 물린 채로 재면 8.60V 가 나온다** (12건 중 6건이 8.6V 초과 · 수신 5.66Hz). 충전 중에는 팩 전압이 만충 위로 올라가므로 **이 값은 고장이 아니다.** 배터리 측정은 반드시 충전기를 분리하고 판단한다 — 물린 채 잰 값으로 규약을 다시 손대면 상한만 계속 올라간다.

한계 — 이 시험은 발을 띄운 짧은 시험이다. 바닥 보행(부하)·장시간 운용·IMU 축은 이 시험의 범위가 아니다.

### 왜 저장소에 넣지 않는가

| 대상 | 라이선스 |
| :--- | :--- |
| 벤더 구동 예제 소스 | 사용한 배포본의 재배포 허가 확인이 필요해 저장소에 포함하지 않음. 일부 새 예제에는 저작권 헤더가 있어 '표기 전무'를 전체 파일로 일반화하지 않음 |
| `MechDog_Arduino` | `license.txt` 는 BSD 인데 **저작권자가 Adafruit** 이다 — 껍데기와 함께 딸려온 파일로 보여 근거로 쓸 수 없다 |
| `MPU6050` | MIT — 명확하다 |
| 외부 센서 라이브러리 | SensorLib v0.2.1은 MIT. 자세 필터는 Arduino 라이브러리 관리자의 Madgwick 1.2.0을 쓴다(벤더 사본에는 GPL 헤더가 있음). 어느 쪽도 저장소에 포함하지 않음 |

명시된 라이선스가 없으면 기본값은 저작권자 전권이다. 상세는 [ADR-20](../../docs/DECISIONS.md).

## 로컬에서 호스트 시험 돌리기

g++ 와 make 가 있으면 저장소 루트에서 한 줄이다. CI(firmware-quality)가 부르는 명령과 같다.

```bash
make -C firmware/mechdog_motion/test test
```

## PC 시험 명령

아래는 시험 도구의 사용법이다. 실제 전원 조건과 설치된 앱을 확인해 적용한다.
재부팅 뒤 수신 시험의 근거는 위 4.1.4 절에 있다.

### 텔레메트리 수신 전용

```powershell
python tools/probe/telemetry_probe.py --bind 0.0.0.0 --port 5101 --duration 30
python tools/probe/telemetry_probe.py --device "DEVICE_ID" --duration 30 --output "new_capture.jsonl"
```

저장소 루트에서 실행하며, `DEVICE_ID`는 송신 대상의 실제 설정값으로 바꾼다.
`--device`를 생략해도 개체·부팅별로 통계를 분리한다.
이 도구는 `MOVE`, `RESET_SAFE` 또는 다른 제어 명령을 보내지 않는다.
다른 프로그램이 UDP 5101을 사용 중이면 포트 충돌을 먼저 해결해야 한다.
출력 파일은 기존 파일을 덮어쓰지 않으며, 부모 폴더는 미리 준비한다.

수신 단조 시각으로 부팅별 `(정상 패킷 수 - 1) / (마지막 시각 - 첫 시각)`을 계산한다.
주기 판정에는 최소 90개 정상 패킷과 10초 관측 구간이 필요하다.
seq 누락·중복·폐기, 새 `boot_id`의 첫 seq와 수신 공백도 기록한다.
첫 seq가 1보다 크면 **시작을 관측하지 못한 것**이며 재부팅 실패로 단정하지 않는다.
평균 주기가 통과해도 큰 공백이 있으면 전체 수신 검사를 통과시키지 않는다.
프로브의 연속성 허용 공백 0.5초는 측정 도구의 기준이며 로봇의 워치독 설정이 아니다.

종료 코드는 0=관측 주기·연속성 통과, 1=수신 없음 또는 검사 실패,
2=표본·기간 부족이나 중단에 따른 미검증이다. 이 결과는 센서 정확도나 WBS 전체 완료 판정이 아니다.
**이 수신 도구만 실행해도 송신 대상/epoch가 설정되지는 않는다.** 실제 새 펌웨어·센서
유효값·Wi-Fi와 같은 PC에서 보낸 유효 명령이 필요하다. 수신 도구는 이를 대신 설정하지 않는다.

### 순정 기체에서 업로드하기 전

COM 포트가 보여도 ESP32가 다운로드 모드에 들어왔다는 뜻은 아니다.
자동 진입 실패 시 버튼·Bluetooth 모듈 배치를 확인한다.
[제조사 Arduino 절차](https://wiki.hiwonder.com/projects/MechDog/en/latest/docs/5.Arduino_Programming_Projects.html)는
다운로드 전 Bluetooth 모듈 제거를 안내한다. 순정 앱의 정상 재부팅은 서보를 초기화할
수 있으므로, 움직임 금지 상태에서 확인되지 않은 버튼/리셋을 반복하지 않는다.

순정 flash 전체와 보정값을 보존하고 파티션 배치를 확인해야 한다. 순정 NVS와
Arduino 기본 업로드 배치가 겹칠 수 있어 백업이 있다고 기본 업로드를 바로 실행하지 않는다.
제조사의 `0x0000` 주소는 전체 배포 이미지 기준이며 Arduino 앱 `.bin` 하나의 주소가 아니다.
이 문서와 빌드 성공은 해당 기체에서 복원이나 플래시 배치를 검증했다는 뜻이 아니다.
앱 영역 기록 방식은 위 4.1.4 절의 경고를 따른다.

### 기존 명령·구동 시험 — 별도 실물 검증 단계

아래 명령은 실제 구동을 포함한다. 센서 진단 준비나 수신 도구 실행의 필수 절차가 아니다.

```powershell
python tools/probe/udp_probe.py 192.168.1.100 --count 100 --interval 0.05
python tools/ops/mechdog_command.py 192.168.1.100 safety
python tools/ops/mechdog_command.py 192.168.1.100 move --step 10 --duration 0.5
python tools/ops/mechdog_command.py 192.168.1.100 watchdog --step 10 --duration 0.4
```

`move`는 10Hz로 명령을 보내고 마지막에 `STOP`을 보낸다. `watchdog`은 마지막
`MOVE` 뒤 송신을 450ms 중단하고 SAFE 잠금과 failsafe 카운터 증가를 확인한다.

## 2.1.3 센서 로그 확인 도구

기존 UART의 `Sensor status:` 행을 PC에서 분석한다. 장치 연결, 명령 송신,
펌웨어 설치 기능은 없다. Python 표준 라이브러리만 사용하며 파일을 줄 단위로 읽고
수치 범위를 누적하는 Host PC의 오프라인 분석 도구다.
실시간 모델 추론이나 ESP32 제어 주기의 성능 개선을 의미하지 않는다.

```bash
python tools/probe/sensor_log_check.py sensor-observe.log --output sensor-analysis.json
```

- 부팅 시작/identity 로그를 기준으로 구간을 분리한다. 중간부터 수신한 구간의
  `boot_id`는 `null`이며 이전 기록의 ID를 추측해서 붙이지 않는다.
- 센서별 유효/무효 행 수·오류 종류·유효 수치의 처음/마지막/최솟값/최댓값을 기록한다.
  잘린 행, 중복 필드, 유효 표시된 NaN/Inf는 측정 통계에 넣지 않는다.
- yaw는 359°→0°를 +1°로 계산한다. 무효/손상 행과 관측된 재부팅 경계에서
  계산 구간을 끊는다. 관측 사이 실제 변화가 180° 미만이라는 전제이며 절대 방위가 아니다.
- 원본 SHA-256을 출력하고 기존 출력 파일은 덮어쓰지 않는다. 추가 UART 필드나
  일반 로그 문장을 결과에 복사하지 않는다.
- 종료 코드 `0`은 관측된 상태 행에 무효/손상이 없다는 뜻이다. `1`은 무효 센서 또는
  손상 행 존재(부팅 중 `starting`/`calibrating`도 포함), `2`는 읽기/쓰기 실패 또는
  해석 가능한 상태 행이 없다는 뜻이다. 어느 코드도 센서 정확도 인증이 아니다.

UART 상태는 약 1초마다 출력하는 best-effort 진단이며 25Hz 원시 샘플 전체가 아니다.
행에 샘플 시간이 없어 Hz, 초당 드리프트, 누락 시간을 계산하지 않는다. 부팅 로그가
누락된 리셋 역시 검출할 수 없다. 기존 `telemetry_probe.py`의 수신 주기 검증과 구분한다.

**2026-09-12 수신 확인:** 구동 OFF 진단 앱에서 UART만 45초 수신해 완전한 상태 행
45개를 얻었다. IMU·초음파·배터리 모두 각 45행 유효, 손상/센서 오류 0행이었다.
pitch −1.260\~−0.568°, roll 0.328\~1.123°, yaw 301.158→307.111°(+5.953°),
ADC 기반 전압 추정 7.668\~7.728V였다. 실제 기체 움직임은 별도 계측하지 않아
yaw 변화의 원인을 확정할 수 없다. 개인 원본 로그는 로컬 측정 보관소에 보관한다.

벤더 예제의 초음파 2바이트 little-endian 값과 mm→cm 단위는 현재 코드와 같다.
벤더는 이동평균과 오류값 치환을 추가하지만 이 진단은 오류와 원시 변화가
보이도록 작은 값 제거나 필터를 넣지 않는다. 초음파 정확도의 근거는 아래 표적 시험이다.

**초음파는 STOP 으로 끝낸 뒤 새로 읽는다 (`read_bytes_after_stop`).** 초음파 모듈의 자체 MCU 는
repeated START 뒤 첫 바이트만 준다 — 레지스터를 쓰고 repeated START 로 이어 읽으면(`read_bytes`)
**거리값의 윗바이트가 늘 0** 이 되어 25.6cm 마다 되감긴다. 벤더 `getDistance` 도 STOP 뒤 새로 읽는다.
2026-09-15\~16 표적 시험(구동·센서 통합 빌드)에서 repeated START 읽기는 20cm 까지는 맞고 30cm 를
3.4\~4.0cm · 50cm 를 21.1cm · 100cm 를 0\~25.5cm 로 읽었으며 최대값이 정확히 25.5cm(255mm)였다.
STOP 뒤 읽기로는 15 → 14.2 · 30 → 28.6 · 50 → 45.8cm 로 안정됐다. IMU 는 repeated START 그대로다.
⚠️ **I²C 장치를 새로 붙이면 이 차이를 먼저 의심한다.** 원자료 `field_tests/results/20260916_2.1.3-sonar/`.
