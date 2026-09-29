# Phase 2 ROS2 경계

```text
LD19 → ESP32 중계 → UDP :5201 → Windows 순찰기(patrol_run) ─┬─▶ guard_scan → 명령 (직접 경로)
                                                            └─▶ 바이트 그대로 복사 → UDP :5203
                                                                                       ↓
                                                                                 scan_bridge → /scan → slam_toolbox
                                                                                       ↑  odom → base_link (5.4.3)
                                                                                       ↑  base_link → laser (마스트 실측)
patrol_run 오도메트리 → UDP :5204 → odom_bridge ─────────────────────────────────────────┘
                              /map · /pose → Windows Python 순찰기 (5.4.4)
```

ROS2는 지도와 위치만 계산한다. 경로계획·구역 선택·보행 명령·안전 래치는 기존
Python 호스트와 MechDog 펌웨어가 맡는다. 컨테이너에는 스캔·오도메트리 링크 디코더만 복사한다.
`odom → base_link`는 `tools/ops/patrol_run.py`가 보내는 ODOM 전문(`docs/PROTOCOL_LIDAR.md`
8절)으로 `odom_bridge`가 발행한다. 실측 오차(정지·직진·선회 3회)와 마스트 값은 아직
없으므로, 아래 명령은 **토픽·tf 경로까지의 사전 검증**이다. 항등 오도메트리로 지도 합격을 꾸미지 않는다.

`docker run` 은 컨테이너의 `/scan` 이 듣는 `5203`을 노출한다 — 중계 노드가 직접
보내는 `5201`이 아니다 (「남은 연결」 5.4.4 절 참고).

## 라이다 없이 검증

저장소 루트에서:

```powershell
docker build -f docker/ros2/Dockerfile -t mechdog-ros2:prelidar .
docker run -d --name mechdog-ros2 -p 5203:5203/udp -p 5204:5204/udp -e LIDAR_DEVICE_ID=lidar-mock mechdog-ros2:prelidar
```

**컨테이너만 검증할 때**(순찰기 없이 브리지 디코더만 확인) — 목업을 `5203`으로 바로 보낸다:

```powershell
python tools/mock/mock_lidar.py --host 127.0.0.1 --port 5203 --device lidar-mock
```

**실제 경로를 검증할 때**(순찰기의 전달까지 포함) — 목업을 기본 `lidar.scan_port`(5201)로
보내고 순찰기를 띄우면, `serve_real()`이 받은 데이터그램을 그대로 `5203`으로 복사해
넘기고 ODOM 을 `5204`로 보낸다. 순찰기는 실기 모드라 먼저 `localization.track: lidar`
(커밋된 값은 `none`)와 `maps/zones.json`(`tools/ops/zone_select.py`)이 있어야 기동한다:

```powershell
python tools/mock/mock_lidar.py --host 127.0.0.1 --device lidar-mock
python tools/ops/patrol_run.py --device <unit-id> --lidar-device lidar-mock
```

다른 터미널에서 ROS2 토픽을 확인한다:

```powershell
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 topic echo /scan --once --field header'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 pkg executables slam_toolbox'
```

## 오도메트리 tf (`odom_bridge` · 5.4.3)

컨테이너는 `scan_bridge`와 `odom_bridge`를 함께 띄운다(둘 중 하나가 죽으면 컨테이너가 끝난다).
`odom_bridge`는 UDP `5204`(`ODOM_PORT`)의 ODOM 전문을 받아 **수신 시각**으로
`odom → base_link`를 낸다. `valid=false`이거나 전문이 `ODOM_STALL_S`(0.5초) 넘게 끊기면
발행을 멈추고 경고한다. `ODOM_DEVICE_ID`를 주면 그 로봇의 전문만 받는다.

```powershell
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 run tf2_ros tf2_echo odom base_link'
```

`base_link → laser`는 `LASER_OFFSET_X_M`·`LASER_OFFSET_Y_M`·`LASER_OFFSET_Z_M`(m)을
**모두** 줄 때만 고정 변환으로 발행한다. 없으면 경고만 하고 발행하지 않는다 — 마스트
실측 전의 항등 변환으로 통과시키지 않기 위해서다. 회전은 넣지 않는다. 장착 방향은
`scan_bridge`의 `LIDAR_MOUNT_YAW_DEG`·`LIDAR_ANGLE_DIRECTION`이 이미 `/scan`에 적용한다.

호스트 쪽 송신은 `tools/ops/patrol_run.py`(실기 모드)가 `lidar.odom_host`·`odom_port`로
`lidar.odom_rate_hz`(10Hz)마다 보낸다. `gait_calibration`이 없는 기체는
오도메트리를 만들지 않고 오류를 남긴다.

끝나면 `docker rm -f mechdog-ros2`. `mock_lidar.py`는 UDP 규약 목업이다. 실물의
UART 타이밍·모터 노이즈·차폐·전원 문제를 검증하지 않는다.

`LIDAR_ANGLE_BINS` 기본 450은 LD19 예상 점 수에 맞춘 **잠정값**이다. 실물의
한 바퀴 점 수·각도 방향을 재서 확정한다. `LIDAR_RANGE_MIN_M`(0.12),
`LIDAR_RANGE_MAX_M`(8.0)도 실측 후 조정한다. ROS 시각은 첫 조각 수신 시각을 쓰며,
중계 노드 `ts`(부팅 후 밀리초)를 epoch로 해석하지 않는다.

실제 2026-09-26 장착 방향은 원시270° 정면·180° 왼쪽·90° 뒤·0° 오른쪽으로
물리 표적 차분 확인했다. 이 기체의 브리지에는 `LIDAR_MOUNT_YAW_DEG=270` 과
`LIDAR_ANGLE_DIRECTION=-1` 을 **함께** 설정해야 한다. 기본 0/+1 은 배치가
없는 목업용이고, 실물 좌표로 간주하면 지도가 뒤집힌다. 이 설정의 ROS 토픽과
마스트 tf·주행 지도 전체 검증은 별도다.

## 남은 연결

- `5.4.3`: 코드(명령 시간 창 × 개체 보행 실측 + IMU yaw 변화량 → 10Hz ODOM →
  `odom_bridge`)는 들어갔다. 남은 것은 정지·직진·좌우 선회 3회 오차 기록, 마스트 실측값
  `LASER_OFFSET_*` 설정, `slam_toolbox`가 스캔 시각의 변환을 조회하는지 확인이다.
- `5.4.4`: 순찰 중에는 Windows 순찰기(`tools/ops/patrol_run.py`)가 UDP `5201`의 **유일한
  수신자**로 정리됐다(`lidar_live_map.py`·`lidar_slam.py` 는 순찰기 대신 따로 켜는 도구다) — 받은 데이터그램을 디코드 성패와 무관하게 바이트
  그대로 `lidar.scan_forward_host:scan_forward_port`(기본 `127.0.0.1:5203`)로
  복사해 컨테이너에 넘긴다. 두 프로세스가 `5201`을 동시에 바인드하려던
  충돌이 이렇게 풀렸다. LiDAR 비상정지(`guard_scan`)는 이 전달과 무관한 직접
  경로로 남는다. **남은 것은** `/map`과 `map → base_link` tf 전달이다. 이동
  중에는 오도메트리로 위치를 갱신하고 정지 스캔으로 보정한다. 이동 중 스캔
  부재는 정상이며, 정지 후 기대한 스캔이 없을 때만 두절로 판단한다.
- `5.4.5`: 실제 LD19·마스트·보행으로 지도, 측위, LOST, 구역 도착을 검수한다.

`slam.yaml`은 `map → odom → base_link → laser` 프레임 이름만 지정한다.
`slam_toolbox`의 지도 검증은 tf가 완성된 뒤 수행한다. RViz2는 이미지에 설치되지만
WSLg 창 표시는 기준 PC의 WSL·Docker GUI 전달 경로에서 별도로 확인해야 한다.
