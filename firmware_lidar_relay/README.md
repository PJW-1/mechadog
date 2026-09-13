# firmware_lidar_relay — LD19 → ESP32 → UDP LiDAR 중계 노드

LD19 의 UART 스트림을 받아 한 바퀴 스캔을 조립하고, `SCAN` JSON 데이터그램을
Host PC 로 UDP 송신하는 **독립 중계 노드**다. MechDog 메인보드를 거치지 않는다
(ADR-6 — 보행 제어 루프를 방해하지 않기 위해).

- 정본 규약: `docs/PROTOCOL_LIDAR.md`
- 배선: `docs/HARDWARE.md` — LD19 Tx → GPIO16 (UART2 RX, 230400 8N1),
  P5V → 5V, GND → GND, PWM 미연결(내부 제어 ~10Hz)
- 대상 보드: ESP32-DevKitC (WROOM). FQBN `esp32:esp32:esp32`, core 2.0.12

## 설정

`wifi_secrets.example.h` 를 `wifi_secrets.h` 로 복사해 채운다
(gitignore 대상 — 커밋 금지):

| 매크로 | 의미 |
| :--- | :--- |
| `MECHDOG_WIFI_SSID` / `MECHDOG_WIFI_PASSWORD` | 공유기 |
| `LIDAR_HOST_IP` / `LIDAR_HOST_PORT` | Host PC 주소 · `config.yaml` 의 `lidar.scan_port`(5201) |

중계 노드는 단방향 송신이라 본체 펌웨어처럼 명령에서 host 를 학습할 수 없어
여기서 정한다.

`device_id` 는 STA MAC 에서 `lidar-<mac12>` 로 자동 생성, `boot_id` 는
부팅마다 64비트 난수 16자리 hex. `ts` 는 노드 uptime ms다 — 스톨 판정은
호스트 도착 시각 기반이라 epoch 정합을 요구하지 않는다.

## MTU 제약 — 부채꼴 청크 송신

`WiFiUDP` 의 TX 버퍼는 MTU(~1460B)다. LD19 한 바퀴(~450점)는 JSON 으로
~6KB 라 **한 데이터그램에 통째로 못 들어간다** — 넘기면 잘려서 깨진
JSON 이 된다. 그래서 펌웨어는 ~72점(≈1.1KB)씩 부채꼴 청크로 잘라보낸다.

각 데이터그램은 그 자체로 규약상 유효한 `SCAN` 이다 — `points` 는 그저
`[angle_deg, dist_mm]` 목록이고, 호스트는 각도 빈(`merge_batch`)으로
재집계하므로 부분 회전도 그대로 받아들인다. `seq` 는 데이터그램마다
단조 증가한다(= Scan ID).

⚠️ `docs/PROTOCOL_LIDAR.md` 6절의 "한 바퀴 전체를 보낸다" 문구는 이
제약과 어긋난다 — ROS2 브리지 작업 시 회전 단위 재조립이 필요하면
그때 정본을 고친다.

## 빌드·플래시

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 --warnings all firmware_lidar_relay
arduino-cli upload -p COM9 --fqbn esp32:esp32:esp32 firmware_lidar_relay
```

시리얼(115200) 진단 로그 5초 주기:
`frames` LD19 프레임 수 · `crc_fail`/`bad_verlen`/`resync` 링크 건전성 ·
`scans` 완성 회전 수 · `sent` UDP 송신 성공 수 · `drop_full` 버퍼 상한 드롭.

`frames=0` 이 계속되면 LD19 미연결·무전원·핀 다름. `crc_fail` 이 불어나면
배선·전원(5V ≥4.5V)을 의심한다.

## 호스트 독립 단위시험

`src/` 의 파서·조립기·인코더는 Arduino.h 없이 컴파일된다
(HAL 분리 원칙). 호스트에 g++ 가 있으면:

```bash
g++ -std=c++17 -Wall -Wextra -O1 -I firmware_lidar_relay/src \
    firmware_lidar_relay/src/ld19.cpp \
    firmware_lidar_relay/src/scan_encoder.cpp \
    firmware_lidar_relay/test/test_lidar_relay.cpp -o build/test_lidar_relay
./build/test_lidar_relay                                   # 단위시험
./build/test_lidar_relay --emit-fixtures | python - <<'PY' # ScanDecoder 교차검증
import json, sys
from host.common.lidar_link import ScanDecoder
dec = ScanDecoder()
for line in sys.stdin:
    assert dec.decode(line).accepted, line
print("decoder accepted all emitted scans")
PY
```

`diagnostics/lidar_self_test` 는 같은 골든 벡터를 **실기에서** 돌리는
스케치다 — 호스트 C++ 컴파일러가 없는 환경의 검증 수단이며, 끝에서 진짜
SCAN 데이터그램을 UDP 로 보낸다. 컴파일 시 상위 `src/` 를 `-I` 로 넣는다:

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 `
  --build-property "compiler.cpp.extra_flags=-I<repo>/firmware_lidar_relay/src" `
  firmware_lidar_relay/diagnostics/lidar_self_test
```
