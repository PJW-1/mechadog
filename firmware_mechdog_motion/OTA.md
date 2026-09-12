# 정지 진단용 Wi-Fi 업데이트

2026-09-12 사용자 승인으로 추가한 선택 기능이다. 기본은
`MECHADOG_ENABLE_OTA=0`이며 현재 **센서 ON / 구동 OFF** 전용이다.
보행 성능이나 AI 모델을 변경하는 기능은 아니다.

## 업데이트 흐름

PC는 로봇 HTTPS 8443의 인증서와 SHA-256 pin을 확인한 뒤 Bearer 인증으로
접속한다. 전체 SHA-256 및 ESP 이미지 검증에 성공해야 다른 OTA 슬롯을
부팅 대상으로 선택한다. 전송 실패는 기존 부팅 대상을 유지하고 재부팅한다.
전송은 120초, 수신 대기는 5초로 제한한다. flash 기록 중 센서 연속 측정은
보장하지 않는다. 모터 초기화/구동 명령은 사용하지 않는다.

새 앱은 `verifyRollbackLater()`로 Arduino의 조기 승인을 미룬다. 기존 센서
유효성, Wi-Fi 및 HTTPS 상태가 5초간 정상이면서 인증된 PC가 확인해야
VALID로 확정한다. 그 전 90초가 지나면 ESP-IDF bootloader가 이전 확정
슬롯으로 복귀한다. 승인 기록도 flash를 쓰므로 확정 후 한 번 더 재부팅해
센서를 초기화한다. PC는 새 실행 이미지/부팅 ID/정상·확정 상태가 2초 연속
유지되는 것을 확인한 뒤 성공을 표시한다.

짧은 `SnapshotBusy`는 새 오류 측정과 구분하지만 마지막 실제 샘플이 기존
기준인 200ms를 넘으면 정상으로 인정하지 않는다. 실제 invalid, timing fault,
네트워크 단절은 정상 판정을 취소한다. 기존 센서 주기·오류 임계는 그대로다.

이 기능은 확정 후 모든 고장이나 이중 고장에 대한 자동 복구를 보장하지
않는다. Wi-Fi 또는 실행 앱이 동작하지 않으면 USB 수동 BOOT 복구가 필요할
수 있다. 09-12에 실패한 공용 TWDT 1초 변경 방식은 사용하지 않는다.

## 09-13 제어 루프 감시 통합

정지용 검증 패키지는 `MECHADOG_ENABLE_TASK_WDT=1`로 독립 루프 감시를 켠다.
SDK TWDT/IWDT는 변경하지 않는다. OTA 전송·확정 중에도 루프 진척750ms 제한을
유지하며, 감시 중지/우회 feed/기한 연장을 하지 않는다. 서보 구동 ON은 허용하지 않는다.
최종본에는 `MECHADOG_WATCHDOG_FAULT_PROBE=0`으로 시험용 UART H 고장주입을 제외한다.

인증된 `/status`는 `loop_watchdog_armed=true`, `loop_watchdog_deadline_ms=750`,
`watchdog_fault_probe=false`를 반환해야 한다. 이미지 SHA/건강/확정/구동OFF 확인과 함께 본다.
같은 감시 소스의 시험본에서 무한루프3회·미확정 앱 고장1회 모두 약813ms 안에
ROM reset 표식을 수신했다. USB/PC 지연을 포함하며 보행 안전성/최악 시간 보장은 아니다.
정상 OTA 설치·확정,4096바이트에서 전송중단 후 기존앱 복구,미확정 시험앱→확정 최종앱
자동 롤백을 실물로 확인했다. 초기 OTA OFF 벤치의 조기 자동확정 문제와 구분한다.
최종 패키지 앱 파일 SHA256은 `ad409f413e3bcec1c2e7289b051094f448201c959ac613a96add469ddff383b3`,
실행 image SHA256은 `33ec3bfa873258f41aa56ea116a278be9598ef33f640b68c6300d243106488ab`이다.
옵션 없는 기본 빌드는 감시 OFF이므로 이 기능이 모든 빌드에서 자동 활성화됐다고 해석하지 않는다.

## 최초 설치와 데이터 보존

순정 4MB flash에는 OTA 슬롯이 없었다. 전체 백업 후 아래 배치로 전환했다.
`ota_partitions.csv`는 검토한 private 빌드에서만 `partitions.csv`로 복사한다.

| 영역 | 시작 | 크기 |
|---|---:|---:|
| NVS | 0x9000 | 0x6000 |
| PHY | 0xf000 | 0x1000 |
| OTA 0 | 0x10000 | 0xf0000 |
| OTA 1 | 0x100000 | 0xf0000 |
| OTA metadata | 0x1f0000 | 0x2000 |
| 순정 FAT VFS | 0x200000 | 0x200000 |

최초 이동은 기존 앱을 OTA 0에 보존하고 OTA 1/metadata를 먼저 검증한 뒤
파티션 표를 마지막에 기록했다. 기존 bootloader, NVS/PHY, 순정 VFS 2MB의
digest를 초기 마이그레이션에서 비교했다. 이후 무선 시험 후 전체 flash를
다시 읽어 비교한 것은 아니다.

**기본 Arduino upload, 전체 erase, 0xe000 boot_app0 기록을 적용하지 않는다.**
일반 배치는 보존 영역과 겹친다. 이 주소는 다른 기체에 그대로 적용할 수
없으며 기체별 배치/MAC/기존 앱/백업 및 복구 경로를 먼저 검토해야 한다.

## 빌드와 PC 도구

검증 SDK: Arduino-ESP32 2.0.12 / ESP-IDF 4.4.5, ESP32 DIO 40MHz, CPU 240MHz.
SDK bootloader rollback을 사용한다. AI 모델은 사용자 PC에서 실행하고,
PC와 메크독 ESP32가 통신한다. 이 업데이트는 PC 추론 코드를 변경하지 않는다.
HTTPS는 ESP32 별도 태스크, PC GUI는 별도 프로세스를 사용한다. CPU 사용률을
정밀 계측한 것은 아니며 실제 센서·telemetry 검증 결과와 구분한다.

private 헤더에 `MECHADOG_OTA_TOKEN`, `MECHADOG_OTA_CERT`, `MECHADOG_OTA_KEY`,
`MECHADOG_OTA_VERSION`을 정의한다. OTA=1/SENSORS=1/ACTUATORS=0 빌드만
사용한다. 실제 BIN은 983040바이트 이하여야 한다. Arduino 기본 보드 용량
표시로 대체하지 않는다. 키/토큰/네트워크 설정과 이를 포함한 BIN/ELF를 Git이나
공개 CI 산출물에 올리지 않는다.

`tools/ota_update.py`의 private config는 host, port, mac, certificate 경로,
certificate_sha256, token을 가진다. package manifest는 application 경로,
application_sha256, image_sha256, mac, ota_protocol=1,
actuators_enabled=false, actuator_off_elf_reviewed=true를 가진다. manifest는
로컬 검토 기록이며 디지털 서명이 아니다. 신뢰하는 PC에서 ELF 구동 OFF
경로를 검토한 패키지만 사용한다.

```text
python tools/ota_update.py status --config PRIVATE_CLIENT.json --report NEW_STATUS.json
python tools/ota_update.py update --config PRIVATE_CLIENT.json --package REVIEWED_PACKAGE.json --report NEW_RESULT.json
```

동일 이미지면 재기록하지 않는다. 중단된 전송을 성공으로 간주하지 말고
전원을 유지한 채 상태를 확인한다. `confirm`은 초기 프로비저닝용이다.
일반 상태 확인은 승인/flash 기록을 하지 않는다.

현재 PC의 바탕화면 **메크독 Wi-Fi 업데이트**는 로봇 IP → 연결 확인 → 검토된
업데이트 파일 선택 → 로봇 업데이트 순서다. 중복 실행을 막고 USB 모드 도구와
작업 잠금을 공유한다. 프로그램/자격정보는 PC 로컬 설치다.

## 검증과 한계

- `test/test_ota_health.cpp`: 시작/5초 판정, 짧은 경합/만료, 실제 오류, 네트워크
  단절 및 millisecond wrap. native CI 검사에 추가했다.
- `tests/test_ota_update.py`: 이미지/메타데이터/기체 검사, 인증서 pin, 조기 거절,
  분할 전송, 동일 이미지 무기록, 마지막 재부팅 전 성급한 완료 방지.
- 2026-09-12 실물 시험 및 실패/수정 이력은 로컬 작업일지와 측정 폴더에
  기록한다. 원격 CI 실행, 보행, 장시간 내구 시험과 구분한다.

실물 ota4 설치·확정 후 재부팅 정상, 승인 미전송 시 다른 슬롯에서 약 100.39초
후 이전 확정 슬롯의 정상 응답 확인, 잘못된 인증/해시 및 4096바이트 전송 후
연결 종료 시험을 통과했다. 마지막 35초 STOP/STATE IDLE 시험은 351건,
10.018Hz, seq 1~351 누락 0, ACK 270건 구동 OFF/SAFE ON이었다. UART 센서
35행 전부 유효, 추가 부팅/panic 0이었다. GUI는 실제 상태 확인/동일 이미지
무기록을 확인했다.

첫 ota4 부팅은 Wi-Fi 접속 재시도와 겹친 592ms 센서 지연이 발생해 승인되지
않았고 이전 앱으로 복귀했다. 재설치는 통과했다. 이 원인의 정밀 분석과
장시간/AP 장애 시험은 남아 있으며 오류 임계 완화로 숨기지 않았다.

기준 API: [Espressif ESP-IDF 4.4.5 OTA](https://docs.espressif.com/projects/esp-idf/en/v4.4.5/esp32/api-reference/system/ota.html).
