# Phase 2 ROS2 경계

```text
LD19 → ESP32 중계 → UDP :5201 → Windows 런타임 또는 patrol_run ─┬─▶ guard_scan → 명령 (직접 경로)
                                                            └─▶ 바이트 그대로 복사 → UDP :5203
                                                                                       ↓
                                                                                 scan_bridge → /scan → slam_toolbox
                                                                                       ↑  odom → base_link (5.4.3)
                                                                                       ↑  base_link → laser (마스트 실측)
런타임·patrol_run 오도메트리 → UDP :5204 → odom_bridge ─────────────────────────────────────────┘
                              map → base_link → pose_bridge → UDP :5205 → Windows 순찰기 (5.4.4)
```

ROS2는 지도와 위치만 계산한다. 경로계획·구역 선택·보행 명령·안전 래치는 기존
Python 호스트와 MechDog 펌웨어가 맡는다. 컨테이너에는 스캔·오도메트리 링크 디코더와
지도 자세 반환 브리지만 둔다.
`odom → base_link`는 본 런타임(`python -m host.runtime --lidar-device ...`) 또는
`tools/ops/patrol_run.py`가 보내는 ODOM 전문(`docs/PROTOCOL_LIDAR.md`
8절)으로 `odom_bridge`가 발행한다. 실측 오차(정지·직진·선회 3회)와 마스트 값은 아직
없으므로, 아래 명령은 **토픽·tf 경로까지의 사전 검증**이다. 항등 오도메트리로 지도 합격을 꾸미지 않는다.

`docker run` 은 컨테이너의 `/scan` 이 듣는 `5203`을 노출한다 — 중계 노드가 직접
보내는 `5201`이 아니다 (「남은 연결」 5.4.4 절 참고).

## 라이다 없이 검증

저장소 루트에서:

```powershell
docker build -f docker/ros2/Dockerfile -t mechdog-ros2:prelidar .
docker run -d --name mechdog-ros2 -p 5203:5203/udp -p 5204:5204/udp `
  -e LIDAR_DEVICE_ID=lidar-mock -e ODOM_DEVICE_ID=mechdog-02 `
  -e MAP_POSE_DEVICE_ID=mechdog-02 mechdog-ros2:prelidar
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

세 브리지는 컨테이너 기본 프로세스로 뜨지만 `slam_toolbox`의 **mapping/localization
모드는 운용자가 선택해서 시작한다.** 새 지도를 만드는 mapping 시험은 다음처럼
실행하고 Jazzy lifecycle의 두 전이를 반드시 보낸다. 노드 이름만 보인다고 활성화된
것이 아니며, activate 전에는 `/map`이 나오지 않는다.

```powershell
docker exec -d mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 launch slam_toolbox online_async_launch.py slam_params_file:=/opt/mechdog/docker/ros2/slam.yaml'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 lifecycle set /slam_toolbox configure'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 lifecycle set /slam_toolbox activate'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 topic echo /map --once --field info'
```

실제 순찰은 mapping 모드에서 즉석으로 만든 지도와 섞지 않는다. 먼저 저장한 ROS2
`*.pgm`+`*.yaml` 쌍을 Windows의 `--maps` 경로에 두고, 같은 지도를 불러온
localization 모드의 `map` 좌표와 맞춘 뒤 사용한다. 이 저장·재적재 실기 절차는
아직 검증 전이므로 로봇 구동 승인 근거로 쓰지 않는다.

## slam_toolbox 기동 — 라이프사이클 전이가 필수다

Jazzy 의 `slam_toolbox` 는 라이프사이클(lifecycle) 노드다. 실행만으로는 `/map`이
광고되지 않는다 — 노드는 `ros2 node list` 에 보이고 로그는 0바이트이며 파라미터는
전부 `Parameter not set` 이라 **증상이 원인을 가린다.** 실행 후 `configure`+`activate`
두 전이를 태워야 `/map`·`/map_metadata` 가 뜨고 `/scan` 구독이 생긴다:

```powershell
docker exec -d mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 run slam_toolbox async_slam_toolbox_node --ros-args --params-file /opt/mechdog/docker/ros2/slam.yaml'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 lifecycle set /slam_toolbox configure && ros2 lifecycle set /slam_toolbox activate'
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && ros2 topic list | findstr map'
```

2026-10-02 실기 확인 — 전이 전에는 `/map` 부재, 두 전이 후 `/map`·`/map_metadata` 광고.

## rviz2 를 WSLg 로 표시

Docker Desktop 컨테이너는 WSLg 의 `/tmp/.X11-unix/X0` 에 닿지 않는다 — 소켓 파일이
파일시스템 경계를 넘지 못하고 `--privileged --pid=host` 도 daemon VM 의 별도
네임스페이스라 안 보인다. 확인된 경로는 **사용자 WSL 배포판의 socat 릴레이**다
(2026-10-02 검증): WSLg 의 X 서버는 같은 VM 안 TCP 연결을 무인증으로 받는다.

최초 1회:

```powershell
wsl --install -d Ubuntu-24.04 --no-launch
wsl -d Ubuntu-24.04 -u root -- apt-get install -y socat
```

WSL 시작 때마다(릴레이는 영구 상주하지 않는다):

```powershell
wsl -d Ubuntu-24.04 -u root -- setsid nohup socat TCP-LISTEN:6000,fork,reuseaddr UNIX-CONNECT:/tmp/.X11-unix/X0 ">nul 2>&1 < /dev/null &"
```

컨테이너는 Ubuntu 배포판의 VM IP 로 X 를 낸다 (`host.docker.internal` 은 Windows
호스트를 가리켜 Ubuntu 리스너에 닿지 않는다):

```powershell
$display_ip = (wsl -d Ubuntu-24.04 hostname -I).Split()[0]
docker run -d --name mechdog-ros2 -p 5203:5203/udp -p 5204:5204/udp -e LIDAR_DEVICE_ID=lidar-mock -e DISPLAY=${display_ip}:0 -e QT_X11_NO_MITSHM=1 mechdog-ros2:prelidar
docker exec mechdog-ros2 bash -lc '. /opt/ros/jazzy/setup.bash && rviz2'
```

`QT_X11_NO_MITSHM=1` 은 필수다 — TCP 릴레이 위에서는 공유메모리 MIT-SHM 확장을
못 쓴다. 재부팅으로 WSL VM 의 IP 가 바뀌면 `$display_ip` 를 다시 읽는다.

## 오도메트리 tf (`odom_bridge` · 5.4.3)

컨테이너는 `scan_bridge`·`odom_bridge`·`pose_bridge`를 함께 띄운다(하나라도 죽으면
컨테이너가 끝난다).
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

호스트 쪽 송신은 본 런타임(`--lidar-device`) 또는 `tools/ops/patrol_run.py`(실기 모드)가 `lidar.odom_host`·`odom_port`로
`lidar.odom_rate_hz`(10Hz)마다 보낸다. `gait_calibration`이 없는 기체는
오도메트리를 만들지 않고 오류를 남긴다.

## 지도 자세 반환 (`pose_bridge` · 5.4.4)

`pose_bridge`는 tf의 `map → base_link`를 10Hz로 읽어 `MAP_POSE` 전문으로 Windows
호스트의 `lidar.map_pose_port`(기본 5205)에 보낸다. `MAP_POSE_DEVICE_ID`는 필수다.
tf가 `MAP_POSE_STALL_S`(기본 0.5초)보다 오래됐거나 조회되지 않으면 `valid=false`를
보내며, 순찰기는 마지막 유효 자세를 갱신하지 않는다. 이후 기존
`localization.pose_timeout_ms`(500ms)가 지나면 `LOST`로 정지한다.

Docker Desktop에서는 목적지 기본값 `host.docker.internal`을 쓴다. 다른 환경은
`MAP_POSE_HOST`를 Windows 호스트 주소로 명시한다. 이 반환 포트는 컨테이너가 받는
포트가 아니므로 `docker run -p`에 추가하지 않는다.

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
- `5.4.4`: 순찰 중에는 Windows 런타임(`--lidar-device`) 또는 순찰기(`tools/ops/patrol_run.py`)가 UDP `5201`의 **유일한
  수신자**로 정리됐다(둘은 `lidar.scan_port` 를 동시에 쥘 수 없어 한 번에 하나만 띄운다. `lidar_live_map.py`·`lidar_slam.py` 는 순찰기 대신 따로 켜는 도구다) — 받은 데이터그램을 디코드 성패와 무관하게 바이트
  그대로 `lidar.scan_forward_host:scan_forward_port`(기본 `127.0.0.1:5203`)로
  복사해 컨테이너에 넘긴다. 두 프로세스가 `5201`을 동시에 바인드하려던
  충돌이 이렇게 풀렸다. LiDAR 비상정지(`guard_scan`)는 이 전달과 무관한 직접
  경로로 남는다. `pose_bridge`가 `map → base_link`를 UDP 5205로 되돌리고 순찰기는
  이 자세만 경로계획에 쓴다. **남은 것은** 같은 저장 지도 좌표계의 `/map`을
  순찰기에 공급하는 운용 절차와 Docker 실통합 검증이다. 이동
  중에는 오도메트리로 위치를 갱신하고 정지 스캔으로 보정한다. 이동 중 스캔
  부재는 정상이며, 정지 후 기대한 스캔이 없을 때만 두절로 판단한다.
- `5.4.5`: 실제 LD19·마스트·보행으로 지도, 측위, LOST, 구역 도착을 검수한다.

`slam.yaml`은 `map → odom → base_link → laser` 프레임 이름만 지정한다.
`slam_toolbox`의 지도 검증은 tf가 완성된 뒤 수행한다. RViz2의 WSLg 표시 경로는
위 「rviz2 를 WSLg 로 표시」 절을 따른다 — 2026-10-02 이 PC에서 창 표시를 확인했다.
