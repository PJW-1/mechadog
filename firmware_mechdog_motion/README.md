# MechDog motion firmware

몸체 ESP32가 UDP 5001번 포트로 JSON 명령을 받고 Hiwonder 보행 API를 호출한다.
명령 형식의 정본은 [`docs/PROTOCOL.md`](../docs/PROTOCOL.md)다.

> **구동·센서 빌드를 하려면 먼저 아래 *"구동·센서 통합 빌드 준비"* 절을 본다.** 벤더 파일은
> 저장소에 없어 각자 받아야 하며(ADR-20), 준비 상태는 `python tools/firmware_env.py` 로 점검한다.

## 4.1.4 현재 상태 — 정지 수신 및 재부팅 2회 통과 (2026-09-12)

**후속 수정 적용:** `fix/4.1.4-reboot-timing`에서는 3초마다 수행하던
`WiFi.reconnect()`의 강제 disconnect를 제거했다. 이미 AP에 연결돼 있으면 DHCP를
기다리고, 미연결이면 `esp_wifi_connect()`로 접속을 요청한다. 기존 3초 간격과 SDK
자동 재접속·NVS 저장 정책은 유지한다. 아래 제한된 재부팅 시험에서 기존 지연 오류가
재발하지 않았지만 모든 네트워크 장애 조건의 해결을 입증한 것은 아니다.
Wi-Fi 이벤트 사유/콜백 도착 시각을 고정 길이 큐로 전달하고, 재시도 반환값·호출 소요 시간을
진단한다. 콜백은 로그 출력이나 재접속을 수행하지 않으며 큐 초과 횟수를 기록한다.
SDK 내부 재접속 처리 이후 도착한 콜백 시각을 실제 무선 이벤트 발생 시각으로 해석하지 않는다.

현재 설치 앱은 **retry1**이다. 기본/센서 ON·구동 OFF 빌드와 ELF 구동 차단 검증을
통과했다(센서 ON flash771265/정적 RAM47532 bytes). 앱777008 bytes의 SHA-256은
`a99c0bd647670a42aee8432ca146027c3e1326164eafb03356a3a919d94cdd88`이다.
자동 다운로드 진입 1회 실패 후 사용자 수동 BOOT 완료로 설치했다. 앱 0x10000만
무압축 기록하고 앱·boot·partition·기존 NVS/PHY·순정 VFS 전후 digest를 검증했다.
설치 직전 snapshot의 순정 보정 9개 타입·값·CRC도 원본과 일치했다.
센서 주기/유효성 임계값·필터·태스크 배치·구동 차단은 변경하지 않았다.

같은 수신 소켓과 decoder를 유지하며 세 구간을 각각 35초씩 측정했다.

| 구간 | 수신 건수 | 관측 Hz | 첫 seq | seq 누락 | 최대 간격 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 초기 부팅 | 351 | 10.00915 | 1 | 2 | 203ms |
| EN 재부팅 1 | 352 | 10.04637 | 1 | 1 | 156ms |
| EN 재부팅 2 | 352 | 10.03746 | 1 | 1 | 156ms |

총 1,055건을 실제 규약 decoder가 수락했고 새 boot의 seq1 수락을 2회 확인했다.
각 구간의 주기/연속성 기준을 통과했다. 리셋 경계의 이전 boot 패킷과 무관한 ACK는
별도로 구분한다. 의도한 재부팅·접속 대기를 포함한 전체 capture 연속성은 fail로 남으며,
이 공백을 지우거나 무손실 송신으로 보고하지 않는다.

UART에서 초기 starting 이후 정상 IMU 상태 166줄을 확인했고 timing fault는 없었다.
AP 접속 이벤트는 약 1.7초, GOT_IP 이벤트는 부팅별 11.4/6.4/34.4초에 도착했다.
DHCP 대기 중 연결을 유지한 경로를 확인했으며, AP 미발견/접속 중단 상황의 재시도 분기는
이번 시험에서 호출되지 않았다. 무선 이벤트 콜백 시각은 실제 무선 발생 시각과 다를 수 있다.
센서값의 전압 범위는 7.744~7.824V였고, 외부 전압계 검증은 남아 있다.
명령상 정지 상태에서도 부팅별 yaw 값 변화가 관측됐다. 실제 외부 움직임을 계측하지 않았고
자이로 시작 보정/드리프트와 축·정확도는 미검증이므로 절대 방위나 정밀 자세로 쓰지 않는다.

**이번에 확인한 범위:** 정지 조건의 10Hz 송신, 동일 수신기의 새 boot seq1 수락과 호스트 로그.
정식 구동/센서 공유 버스 통합, 장시간·AP 장애 시험과 센서 정확도 검증은 남아 있다.
WBS 공식 완료 표시는 변경하지 않았다. 시험 후 COM/UDP를 닫았으며 구동 OFF를 유지했다.
원본 로그/설치 보고서 4개와 파생 분석은 로컬 2026-09-12 측정 기록에 SHA 대조해 보관했다.

### 이전 timing1 시험 이력

앞서 **timing1** 진단 앱을 설치했으며 당시 앱 SHA-256은
`4f98309f9bf7629390d635ea2e239a0efb89722cafd2ae1a28f5a01c90a80fb2`다.
센서 ON·구동 OFF, CPU 240MHz·DIO 40MHz·4MB이며 ADC 보정과 최초 지연 진단이 포함됐다.
앱 0x10000만 명시적 무압축으로 기록하고 앱·부트·파티션·기존 NVS/PHY·순정 VFS의
digest를 대조했다. 설치 직전 snapshot의 순정 보정 9개도 타입·값·CRC가 일치했다.

본체 ON·PC USB 연결, 충전기 분리라는 사용자 보고 조건에서 다음을 직접 확인했다.

- 첫 35초: 실제 FR-5.2 센서 패킷 **351건, 평균 10.02693Hz**, 첫 seq=1.
  seq1~353 중 누락 2개, 최대 수신간격 141ms. 주기·연속성 검사 통과가 무손실을 뜻하지는 않는다.
- 같은 수신 소켓·decoder를 유지한 EN 1회 재부팅: 새 UART boot ID는 확인했으나
  부팅 tick 6563ms에 센서 작업이 예정 기상보다 **532ms 늦게 실행**돼 IMU가 무효화됐다.
  `imu_timing=wake_late`, 단계 mask=0이므로 특정 센서 읽기에 532ms가 걸렸다는 뜻은 아니다.
- 이후 Wi-Fi가 복구돼 STOP/STATE ACK 272건을 받았지만 **새 boot 텔레메트리는 0건**이었다.
  리셋 경계 뒤 받은 1건은 이전 boot seq354의 잔여 패킷이다. 새 boot seq1 수락은 미검증이다.
- ADC eFuse Vref와 공식 분압비 4 적용 후 유효 진단값은 **7.952~8.064V**였다.
  외부 계측기로 검증하지 않아 배터리 건강이나 절대 정확도를 판정하지 않는다.

이번 송신은 STOP/STATE IDLE뿐이며 구동 OFF·SAFE ON을 유지했다. 재부팅 시험 실패 후
추가 리셋을 반복하지 않았다. Wi-Fi 미접속과 센서 지연의 인과관계는 아직 확정되지 않았고
주기·오류 임계값·Wi-Fi 저장 및 재접속 정책은 바꾸지 않았다. **WBS 4.1.4 전체 완료는 아니다.**
재부팅 안정성, 센서 정확도와 구동/센서 공유 버스 통합은 남아 있다.
원본 로그 4개와 파생 분석은 로컬 측정 폴더의 2026-09-12 기록에 해시 검증해 보관했다.
이번에는 준비된 빌드를 설치했으며 새로운 소스 컴파일·단위시험·원격 CI를 수행하지 않았다.

**Git 업로드 전 재검증(09-12):** 전체 Python 시험과 커버리지 게이트 통과(86.82%),
ruff 검사·포맷·WBS 생성 문서 일치 확인. C++11 encoder를 다시 컴파일해 5,412개 검사와
Python decoder의 실제 출력 18개·선택 필드 3개 대조를 통과했다. 펌웨어 포맷 검사와
Cppcheck 2.21.0도 확인했다. Cppcheck는 기존 `motion_hal.cpp`의 `functionStatic`
스타일 알림만 로컬에서 제외했으며 CI 설정에는 제외를 추가하지 않았다.
하드웨어 추가 조작이나 원격 CI는 이 재검증에 포함되지 않는다.

## 이전 진단 이력 (2026-09-11 당시 상태)

아래 미설치·대기·중단 표현은 09-11 각 단계의 이력이다. 현재 상태는 위 09-12 결과를 따른다.

사용자가 연결한 충전기의 OUTPUT을 **12.6V**라고 보고했다.
[공식 충전 안내](https://wiki.hiwonder.com/projects/MechDog/en/latest/docs/1.Getting_Ready.html)의
**8.4V 충전기**와 맞지 않아 실물 업로드·통신·시험을 모두 중단했다.
이후 사진에서 다른 Hiwonder 충전기의 **8.4V·2A 정격**을 확인했고 사용자는 연결선 분리와
외관상 이상 없음을 보고했다. 충전기 분리·본체 OFF를 안내한 뒤 PC USB만 재연결하여
기존 UART 로그를 수신했다. 현재는 수신을 끝내고 PC에서 진단 코드를 보완 중이다.
본체 스위치 위치를 직접 관찰하거나 실제 배터리 전압을 측정하지는 않았다.
충전기 라벨이나 미보정 ADC 값만으로
실제 배터리 전압, 손상 여부 또는 기체의 사용 가능 상태를 판정하지 않는다.

다음 구성 요소를 준비했다.

- `src/telemetry_encoder.*`: 규약 검사, JSON 직렬화, 개체·부팅별 송신 순번.
- `src/telemetry_publisher.*`: 별도 UDP 소켓으로 PC의 5101 포트에 보내는 주기 송신 구성 요소.
- `src/sensor_hal.*`: 본체 센서 취득, 유효성·신선도와 오류 상태를 제공하는 진단용 HAL.
- [`tools/telemetry_probe.py`](../tools/telemetry_probe.py): PC에서 수신 기록과 주기를 확인하는 도구.

**센서 취득과 주기 송신 호출을 `firmware_mechdog_motion.ino`에 연결했다.**
유효 명령 수신 시 PC 주소·epoch 시각을 설정하고 watchdog 처리 뒤 센서 스냅샷을
송신한다. `PING`·폐기 패킷은 시각/목적지/명령 나이를 갱신하지 않는다.

마지막으로 설치·실행한 앱은 **perf1**이며 SHA-256은 다음과 같다.

```text
bbc22cbeb9b9081e5bf09091c2c53b348fbb829a5c58a57ecbc3643b728d033e
```

perf1의 앱 기록과 부트로더·파티션·현재 NVS·PHY·순정 VFS 보호 영역 검증을 마쳤다.
UART에서 **actuator OFF, CPU 240MHz, UART TX 버퍼 1,024 bytes**를 확인했다.
flash 설정은 **DIO·40MHz·4MB**다. CPU 속도와 flash 속도는 다른 설정이다.
IMU와 초음파는 유효한 값을 취득했다. **35.015초** 정지 시험에서 STOP/STATE에 대한
**ACK 251건, 텔레메트리 0건**을 받았고, UART에서 송신 거부 이유
`battery outside [6.0,8.4]`를 확인했다. 앞선 ACK 254건·센서 unavailable/stale 결과와
구분되는 후속 시험이다. IMU·초음파 취득 확인은 축·정확도 검증 완료를 뜻하지 않는다.

이후 **PC USB만 연결한 12초 수신**에서는 상태 로그 12줄과 초음파 유효값을 받았지만,
IMU는 `timing_fault`가 유지됐다. 그 오류가 처음 발생한 순간은 이번 로그에 없으며
센서 하드웨어 불량이나 전원 부족으로 원인을 확정할 수 없다. USB만 연결하고 본체 OFF를
안내한 조건에서 나온 기존 전압 추정치도 배터리 단자 전압으로 사용하지 않는다.

사용자가 **본체 전원을 켠 뒤** 12초 재수신했을 때 ADC 원시값은 2401~2415로 변했지만
기존 IMU 오류는 유지됐다. 현재 설치된 구동 OFF 앱을 EN 1회로 재시작하고 30초를
수신하자, 초기화 후 정상 IMU 기록 16줄이 나온 다음 `timing_fault`가 다시 발생했다.
같은 캡처에 거리·전압의 일시적 stale과 최대 메인 루프 간격 211,376µs가 관측됐다.
전원 ON 상태에서도 재현된 오류이며, USB만 켠 당시 최초 오류의 원인까지 확정한 것은
아니다. 새 부팅 ID와 actuator OFF 로그를 확인했고 이동 명령이나 플래시 쓰기는 하지 않았다.
최초 지연을 기록할 준비된 진단 앱은 **수동 다운로드 진입 대기 중**이다.

코드 검토에서 기존 3초 수동 재접속과 라이브러리 자동 재접속 경로가 함께 존재함을
확인했다. 실제 지연과의 인과관계는 미검증이며 이번에는 Wi-Fi 저장·재접속 정책을
바꾸지 않았다. 반복 설정 호출이 항상 실제 flash 쓰기나 지연을 발생시킨다고 단정하지 않는다.

설치된 perf1의 배터리 값은 공식 예제의 `raw * 3.6` 계수를 사용한 **미보정 mV 추정치**다.
이 값을 실제 배터리 전압으로 확정하지 않는다. 텔레메트리의 배터리 허용 범위
`[6.0, 8.4]`를 넓히거나 측정값을 가짜 정상값으로 바꾸지 않았다.
실제 10Hz 송신·재부팅 순번 검증과 WBS 4.1.4 완료는 아직 충족하지 못했다.

설치 이력에서 **ROM 압축 기록과 perf1의 공식 stub 기록을 구분한다.** 첫 ROM 압축 기록은
보호 영역 0x6000~0x8fff를 변경했고, 앱 실행 전에 원본 세 섹터를 무압축 복원한 뒤
앱·부트로더·파티션·NVS·PHY·VFS 대조를 모두 통과했다. 이후 perf1은 공식 stub의
기본 압축 전송으로 기록됐으며 전후 보호 영역 검증을 통과했다. 사용한 도구에서는
`compress=False`만 전달해도 stub의 기본 압축을 해제하지 못했으므로, perf1을
무압축 설치로 기록하지 않는다. NVS·순정 VFS를 보존하므로 기본 Arduino 파티션이나
boot_app0를 포함한 일반 업로드를 적용하지 않는다.

**설치되지 않은 후속 소스:** 배터리 변환을 같은 ADC raw 샘플의
`esp_adc_cal_raw_to_voltage()` 결과에 공식 회로의 분압비 **4**를 곱하는 방식으로 수정했다.
eFuse two-point/Vref 보정을 우선 사용하고, 없으면 기본 Vref로 변환하며 그 출처를
진단에 표시한다. raw 값과 보정 출처를 유지하며, 외부 계측기로 전체 경로를 검증한
`battery_calibrated` 상태는 여전히 false다. ADC 보정은 충전 전압을 제어하는 기능이 아니다.
UART 후속 소스는 출력 공간에 **128 bytes 추가 여유**를 요구하고, 진단 출력 누락 횟수를
기록하며 출력하지 못한 구간의 최대 루프 간격도 보존한다. 이때 생략하는 것은 진단 로그이며
센서 샘플이나 텔레메트리가 아니다. 이 ADC·UART 후속 수정은 모두 **기체 미설치**이고,
실기 검증을 진행하지 않았다. 변경 후 Arduino core 2.0.12/DIO40 오프라인 빌드는
기본 설정 flash 733,709 / 정적 RAM 46,288 bytes,
센서 ON·구동 OFF 설정 flash 768,649 / 정적 RAM 47,196 bytes로 통과했다.
후자의 ELF에서 구동 OFF 경로를 재확인했고, 관련 Python 시험 50건과 포맷·diff 검사를
통과했다. 외부 계측과 실제 송신 검증은 전원·배터리 상태 확인 후에만 재개한다.

**추가 소스 진단(미설치):** 최초 IMU timing fault의 조건과 발생 시각, 예정 기상 대비 지연,
해당 회차의 경과 시간 및 IMU/필터·초음파·ADC 단계의 경과 시간을 보존한다.
`imu_timing`은 `wake_late`, `cycle_elapsed`, `scheduled_elapsed`를 구분하며
`imu_stage_mask`의 비트 1/2/4는 각각 IMU·필터/초음파/ADC 시간의 측정 여부다.
mask에서 빠진 단계의 0은 측정 결과가 아니다. 단계 시간에는 선점·인터럽트·드라이버 대기가
포함되므로 특정 부품의 고장이나 CPU 사용 시간으로 단정하지 않는다. 이 기록은 최초 오류
시점의 값으로 이후 정상 회차가 덮어쓰지 않는다. 기존 오류 조건·주기·무효화 정책은 유지한다.
추가 진단까지 포함한 Arduino core 2.0.12/DIO40 빌드는 기본 설정
flash 734,041 / 정적 RAM 46,288 bytes, 센서 ON·구동 OFF 설정
flash 769,405 / 정적 RAM 47,244 bytes로 통과했다. 센서 ON 이미지의 DIO40/4MB 헤더와
ELF 구동 OFF 경로, 변경 파일의 포맷·diff 검사를 확인했다. 이 수치는 위 ADC 단독
수정본 이후 결과이며 **ADC·최초 지연 진단을 포함한 새 앱은 여전히 기체 미설치**다.

앞선 소스의 검증 이력에는 Arduino core 2.0.12 기본/센서 ON·actuator OFF 빌드,
C++11 encoder 검사 5,412건, Python decoder의 C++ 출력 18건 및 선택 필드 3건 수락,
관련 Python 시험 50건·ruff와 Cppcheck 2.21.0 검사가 있다. 이는 이번 후속 수정의
검증 결과를 대신하지 않는다. Cppcheck의 기존 `MotionHal::actuators_enabled`
`functionStatic` 스타일 알림 한 건만 로컬에서 제외했으며 CI 제외나 기존 HAL API 변경은 없다.
원격 CI는 수행하지 않았다. 송신 코어·수신 도구 단위시험은 실물 센서 정확도나 송신 성공을
뜻하지 않으며, 실행 중 전체 CPU 부하·메모리와 구동 공존도 검증 완료로 표시하지 않는다.

기본값은 `MECHADOG_ENABLE_SENSORS=0`, `MECHADOG_ENABLE_ACTUATORS=0`이다.
**2026-09-12부터 두 기능을 함께 켤 수 있다** — I²C 0번 포트는 센서 HAL이 쥐고 벤더의 I²C
기능은 부르지 않는다(아래 *"구동·센서 통합 빌드 준비"* 5단계). 통합 빌드는 컴파일까지
확인했고 보행 중 공존은 실기 검증 전이다.
센서 측정 유효 플래그와 본체 축 검증·배터리 보정 완료 여부는 서로 다르다.
실제 보정, 축·부호 검증, 순정 복원 시험과 정식 통합은 남아 있다.
현재 yaw는 필터 기준 상대 자세이며 나침반의 절대 방위가 아니다.
필터 시작 시 정지 상태의 자이로 오프셋을 취득하며, 시작 yaw가 0도라는 보장은 없다.
필터 시작 후 취득 실패나 전체 주기 누락이 발생하면 진단 HAL은 IMU를 재부팅까지
무효화한다. 운용 중 복구 정책과 실제 스케줄 지연은 정식 통합 전에 검증해야 한다.

센서값과 실제 안전 로직 발동 상태를 구분한다. `safety_latched`는 기존 실제 래치,
`state`는 래치 중 `FAILSAFE`, 그 외 수신 `STATE` 반향이다. `RESET_SAFE` 성공 시
`IDLE`로 복귀한다. 배터리 경고는 정본의 7.0V 이하 조건이며 셧다운 구현은 아니다.
`link_ok`는 유효 명령을 받고 3초 이내인 상태로, 300ms 보행 watchdog과 구분한다.
전도 자동 정지는 P2로 이연됐으므로 `tipped=false`는 해당 정지가 발동하지 않았다는
뜻이며 정상 자세를 보증하지 않는다. 장애물 자동 정지는 미구현이므로 선택 필드
`obstacle`을 생략한다. 거리가 가까운 것만으로 정지가 발동했다고 보고하지 않는다.
publisher의 시각은 수락한 명령의 epoch를 수신 시각에 맞춰 추정하므로 명령 전송 지연이
포함된다. 정확한 시각 동기화, 센서 오류 시 보고 방식과 보행 HAL의 공유 버스 연결은 미검증이다.

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

MechDog의 로컬 검증과 CI 빌드는 **ESP32 Arduino core 2.0.12 / ESP-IDF 4.4.5**를
기준으로 하며, 빌드 재현성을 위해 core 버전과 DIO·40MHz 설정을 고정한다.
ESP32 보드 매니저 인덱스를 등록한 환경에서 같은 버전을 설치한다.

```powershell
arduino-cli core install esp32:esp32@2.0.12
arduino-cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" firmware_mechdog_motion
```

위 `compile` 명령은 DIO·40MHz로 컴파일만 하며 업로드를 수행하지 않는다.
CI의 XIAO-Vision 타깃은 기존의 최신 ESP32 core 설치 정책을 유지한다.
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
  예제 자료의 사본 대신 이것으로 센서 빌드가 `--warnings all`로 컴파일됐다(2026-09-12).

설치 명령은 아래 *"구동·센서 통합 빌드 준비"* 2단계에 있다.
현재 CI의 MechDog 컴파일은 **센서 OFF / actuator OFF 기본 빌드**다.
센서 ON 경로는 외부 SensorLib와 Madgwick이 필요하며, 이 의존성을 준비하는
CI 단계는 아직 없다. 따라서 CI 성공은 센서 ON 코드의 컴파일, 실제 센서 취득,
10Hz 송신이나 구동 공존을 검증한 결과가 아니다. 센서 ON 빌드는 위의 core 버전과
아래 명령으로 로컬에서 별도 확인한다.

이 외부 소스를 저장소에 자동으로 복사하거나 임의의 다른 버전으로 바꾸지 않는다.
라이브러리 탐색과 빌드 구성을 먼저 확인한 뒤, 센서 진단 설정으로 컴파일한다.

```powershell
arduino-cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" --build-property "compiler.cpp.extra_flags=-DMECHADOG_ENABLE_SENSORS=1 -DMECHADOG_ENABLE_ACTUATORS=0" firmware_mechdog_motion
```

센서 ON 설정은 연결된 초기화·취득 경로를 켠다. 부팅 후 정지 상태의 오프셋 취득과
모든 센서의 유효값이 필요하며, 오류 중에는 정상 텔레메트리를 만들지 않는다.
Wi-Fi는 위에서 선택한 설정으로 접속하고 재접속은 루프에서 처리한다. 부팅 시 20초 대기는 제거했다.

## 구동·센서 통합 빌드 준비 — 벤더 파일·라이브러리·보드 패키지

**저장소에 없는 파일이 필요하다.** Hiwonder 예제 소스에 라이선스 표기가 없어 재배포할 수
없기 때문이다 ([ADR-20](../docs/DECISIONS.md)). 각자 받아 두고, **빌드 전에 점검 도구로 확인한다.**

> **2026-09-12 · 이 절차 그대로 센서+구동 통합 빌드가 컴파일됐다** (core 2.0.12 ·
> flash 800,921 B (61%) · 정적 RAM 49,092 B). **보행 중 센서·텔레메트리 공존은 실기로 아직
> 확인하지 않았다** — 아래 6단계.

### 0단계 — 점검부터 한다

```powershell
python tools/firmware_env.py
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

MechDog 는 core **2.0.12** 로 고정이고 XIAO 는 최신 core 를 쓴다. 한 Arduino 설정에 두 버전을
둘 수 없으므로 **MechDog 전용 폴더**(`%USERPROFILE%\.arduino-mechdog`)를 쓴다 — 쓰던 IDE
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
firmware_mechdog_motion/
├── firmware_mechdog_motion.ino
├── HW_MechDog.cpp   HW_MechDog.h      ← 벤더 (git 무시)
├── Hiwonder.cpp     Hiwonder.h
├── Servo.cpp        Servo.h
├── WMMatrixLed.cpp  WMMatrixLed.h
├── pwm_servo.cpp    pwm_servo.h
├── action.h
└── src/                               ← 우리 파일
```

`src/motion_hal.cpp` 는 `"../HW_MechDog.h"` 로 부른다. `src/` 의 파일에서는 스케치 루트가
포함 경로에 없어서, 예전 안내대로 `"HW_MechDog.h"` 로 두면 **파일이 있어도 못 찾았다**
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
> 해시의 기준은 `tools/firmware_env.py` 의 `VENDOR_FILES` 다(2026-09-12 통합 빌드를 통과한
> 조합). 다른 배포본이면 해시가 다르게 나오는데, **틀렸다는 뜻이 아니라 검증하지 않은 조합**이라는
> 뜻이다 — 통합 빌드와 실기 확인을 다시 거친 뒤 표를 갱신한다.

### 4단계 — 컴파일

```powershell
# 센서 + 구동 (통합). 구동만이면 SENSORS=0, 센서만이면 ACTUATORS=0
& $cli compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" --build-property "compiler.cpp.extra_flags=-DMECHADOG_ENABLE_SENSORS=1 -DMECHADOG_ENABLE_ACTUATORS=1" firmware_mechdog_motion
```

⚠️ **벤더 파일이 스케치 폴더에 있으면 `--warnings all` 을 쓰지 않는다.** 벤더 코드의 경고
(반환값 누락 · 괄호 · 미사용 변수)가 오류로 바뀌어 **구동을 끈 빌드까지** 실패한다 — 스케치
루트의 `.cpp` 는 설정과 무관하게 항상 함께 컴파일되기 때문이다. CI 와 같은 조건으로 **우리
코드만** 확인하려면 벤더 파일이 없는 사본에서 컴파일한다.

```bash
git ls-files -z firmware_mechdog_motion | xargs -0 cp --parents -t <빈 폴더>
```

### 5단계 — I²C 규칙: 벤더의 I²C 기능은 부르지 않는다

센서 HAL 이 **I²C 0번 포트(SDA22/SCL23)** 를 쥔다. 벤더도 같은 포트·핀을 `IIC1` 로 감싸지만
그것을 여는 곳은 `homeostasis()`(자세 균형)·`UltrasoundSonar`·`MP3Sensor` 뿐이고, 우리가 쓰는
`MechDog_init()`·`move()` 는 I²C 를 쓰지 않는다(벤더 소스 2024-08 판과 미리 컴파일된 라이브러리의
기호로 확인). 그래서 **그 셋을 우리 코드에서 부르지 않는 것**이 통합의 조건이며
`tests/test_firmware_vendor_boundary.py` 가 막는다. 앞으로 I²C 장치를 붙일 때도(WonderEcho 등)
벤더 클래스가 아니라 센서 HAL 의 버스를 통한다.

벤더는 부팅 때 **배터리 감시 태스크**도 띄운다 — GPIO34 를 0.1초마다 읽고 `raw×3.6` 이 7.0V
미만이면 부저를 울린다. 우리 센서 HAL 도 같은 핀을 읽는다. ADC 접근은 드라이버가 순서를
맞추지만 실기로는 아직 보지 않았다.

### 6단계 — 업로드와 첫 실기 확인

이 절은 **컴파일까지**다. 업로드는 아래 *"순정 기체에서 업로드하기 전"* 절을 따른다 — 일반
업로드는 NVS 의 보정값을 덮을 수 있다. ⚠️ **업로드하면 `MechDog_init()` 이 서보를 초기화한다.**
처음에는 몸통을 받쳐 **네 발을 띄운 상태**로 시험한다. 확인할 것:

- 걷는 중에도 텔레메트리가 10Hz 로 오는가 (`tools/telemetry_probe.py`)
- UART 에 `imu_timing` 오류가 없는가 — 센서 태스크는 40ms 늦으면 **재부팅까지 IMU 를 끄고**,
  그러면 텔레메트리 전체가 멈춘다. 보행 계산과 같은 코어에서 돈다
- 배터리 값이 벤더 부저 경고와 어긋나지 않는가

### 왜 저장소에 넣지 않는가

| 대상 | 라이선스 |
| :--- | :--- |
| 벤더 구동 예제 소스 | 사용한 배포본의 재배포 허가 확인이 필요해 저장소에 포함하지 않음. 일부 새 예제에는 저작권 헤더가 있어 기존의 '표기 전무' 설명을 전체 파일로 확대하지 않음 |
| `MechDog_Arduino` | `license.txt` 는 BSD 인데 **저작권자가 Adafruit** 이다 — 껍데기와 함께 딸려온 파일로 보여 근거로 쓸 수 없다 |
| `MPU6050` | MIT — 명확하다 |
| 외부 센서 라이브러리 | SensorLib v0.2.1은 MIT. 자세 필터는 Arduino 라이브러리 관리자의 Madgwick 1.2.0을 쓴다(벤더 사본에는 GPL 헤더가 있음). 어느 쪽도 저장소에 포함하지 않음 |

명시된 라이선스가 없으면 기본값은 저작권자 전권이다. 상세는 [ADR-20](../docs/DECISIONS.md).

## PC 시험 명령

아래는 시험 도구의 사용법이다. 실제 전원 조건과 설치된 앱을 확인해 적용한다.
현재 설치 앱(retry1)은 구동 OFF 정지 진단용이다. 재부팅 후 IMU 오류는 retry1의 제한 시험에서
재발하지 않았다(맨 위 4.1.4 절).

### 텔레메트리 수신 전용

```powershell
python tools/telemetry_probe.py --bind 0.0.0.0 --port 5101 --duration 30
python tools/telemetry_probe.py --device "DEVICE_ID" --duration 30 --output "new_capture.jsonl"
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
이번 USB 연결 작업에서는 이동·자세·서보 초기화와 구동 펌웨어 업로드를 수행하지 않는다.

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

최초 ROM 기록의 보호 영역 불일치는 원본 복원과 재검증을 마쳤고, perf1 앱 기록 때도
보호 영역 검증을 통과했다. 이것은 순정 펌웨어 전체 복원 시험이나 기체 정상 판정이 아니다.
이후 정격 8.4V 충전기 사진과 충전기 분리 보고를 확인하고 timing1 ADC·UART 진단 앱을
설치했다. 사용자 본체 ON 보고 조건에서 위 정지 시험을 수행했으며 배터리 실측은 남아 있다.

### 기존 명령·구동 시험 — 별도 실물 검증 단계

아래 명령은 실제 구동을 포함한다. 센서 진단 준비나 수신 도구 실행의 필수 절차가 아니다.

```powershell
python tools/udp_probe.py 192.168.1.100 --count 100 --interval 0.05
python tools/mechdog_command.py 192.168.1.100 safety
python tools/mechdog_command.py 192.168.1.100 move --step 10 --duration 0.5
python tools/mechdog_command.py 192.168.1.100 watchdog --step 10 --duration 0.4
```

`move`는 10Hz로 명령을 보내고 마지막에 `STOP`을 보낸다. `watchdog`은 마지막
`MOVE` 뒤 송신을 450ms 중단하고 SAFE 잠금과 failsafe 카운터 증가를 확인한다.
