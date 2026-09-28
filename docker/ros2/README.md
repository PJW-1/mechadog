# Phase 2 ROS2 경계

```text
LD19 → ESP32 중계 → UDP :5201 → Windows 순찰기(patrol_run) ─┬─▶ guard_scan → 명령 (직접 경로)
                                                            └─▶ 바이트 그대로 복사 → UDP :5203
                                                                                       ↓
                                                                                 scan_bridge → /scan → slam_toolbox
                                                                                       ↑  odom → base_link (5.4.3)
                                                                                       ↑  base_link → laser (마스트 실측)
                              /map · /pose → Windows Python 순찰기 (5.4.4)
```

ROS2는 지도와 위치만 계산한다. 경로계획·구역 선택·보행 명령·안전 래치는 기존
Python 호스트와 MechDog 펌웨어가 맡는다. 컨테이너에는 스캔 규약 디코더만 복사한다.
`slam_toolbox`가 요구하는 `odom → base_link`를 아직 제공하지 않으므로, 아래
명령은 **스캔 토픽까지의 사전 검증**이다. 항등 오도메트리로 지도 합격을 꾸미지 않는다.

`docker run` 은 컨테이너의 `/scan` 이 듣는 `5203`을 노출한다 — 중계 노드가 직접
보내는 `5201`이 아니다 (「남은 연결」 5.4.4 절 참고).

## 라이다 없이 검증

저장소 루트에서:

```powershell
docker build -f docker/ros2/Dockerfile -t mechdog-ros2:prelidar .
docker run -d --name mechdog-ros2 -p 5203:5203/udp -e LIDAR_DEVICE_ID=lidar-mock mechdog-ros2:prelidar
```

**컨테이너만 검증할 때**(순찰기 없이 브리지 디코더만 확인) — 목업을 `5203`으로 바로 보낸다:

```powershell
python tools/mock_lidar.py --host 127.0.0.1 --port 5203 --device lidar-mock
```

**실제 경로를 검증할 때**(순찰기의 전달까지 포함) — 목업을 기본 `lidar.scan_port`(5201)로
보내고 순찰기를 띄우면, `serve_real()`이 받은 데이터그램을 그대로 `5203`으로 복사해
넘긴다:

```powershell
python tools/mock_lidar.py --host 127.0.0.1 --device lidar-mock
python tools/patrol_run.py --device <unit-id> --lidar-device lidar-mock
```

다른 터미널에서 ROS2 토픽을 확인한다:

```powershell
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 topic echo /scan --once --field header'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 pkg executables slam_toolbox'
```

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

- `5.4.3`: 시연 기체 명령 시간 창과 개체별 보행 실측을 적분해 이동 중에도 10Hz로
  `odom → base_link`를 발행하고, 마스트 치수로 `base_link → laser`를 고정한다.
- `5.4.4`: Windows 순찰기(`tools/patrol_run.py`)가 UDP `5201`의 **유일한
  수신자**로 정리됐다 — 받은 데이터그램을 디코드 성패와 무관하게 바이트
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
