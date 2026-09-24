# Phase 2 ROS2 경계

```text
LD19 → ESP32 중계 → UDP :5201 → scan_bridge → /scan → slam_toolbox
                                                   ↑  odom → base_link (5.4.3)
                                                   ↑  base_link → laser (마스트 실측)
                              /map · /pose → Windows Python 순찰기 (5.4.4)
```

ROS2는 지도와 위치만 계산한다. 경로계획·구역 선택·보행 명령·안전 래치는 기존
Python 호스트와 MechDog 펌웨어가 맡는다. 컨테이너에는 스캔 규약 디코더만 복사한다.
`slam_toolbox`가 요구하는 `odom → base_link`를 아직 제공하지 않으므로, 아래
명령은 **스캔 토픽까지의 사전 검증**이다. 항등 오도메트리로 지도 합격을 꾸미지 않는다.

## 라이다 없이 검증

저장소 루트에서:

```powershell
docker build -f docker/ros2/Dockerfile -t mechdog-ros2:prelidar .
docker run -d --name mechdog-ros2 -p 5201:5201/udp -e LIDAR_DEVICE_ID=lidar-mock mechdog-ros2:prelidar
python tools/mock_lidar.py --host 127.0.0.1 --device lidar-mock
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

## 남은 연결

- `5.4.3`: 시연 기체 명령 시간 창과 개체별 보행 실측을 적분해 이동 중에도 10Hz로
  `odom → base_link`를 발행하고, 마스트 치수로 `base_link → laser`를 고정한다.
- `5.4.4`: 컨테이너가 UDP `5201`의 **유일한 수신자**가 되도록 기존 Windows
  순찰기의 스캔 입력을 정리하고, `/map`과 `map → base_link` tf를 전달한다.
  이동 중에는 오도메트리로 위치를 갱신하고 정지 스캔으로 보정한다. 이동 중
  스캔 부재는 정상이며, 정지 후 기대한 스캔이 없을 때만 두절로 판단한다.
  현재 `tools/patrol_run.py`도 `5201`을 직접 바인드하므로 두 프로세스를 동시에
  켜면 같은 데이터를 안정적으로 받을 수 없다.
- `5.4.5`: 실제 LD19·마스트·보행으로 지도, 측위, LOST, 구역 도착을 검수한다.

`slam.yaml`은 `map → odom → base_link → laser` 프레임 이름만 지정한다.
`slam_toolbox`의 지도 검증은 tf가 완성된 뒤 수행한다. RViz2는 이미지에 설치되지만
WSLg 창 표시는 기준 PC의 WSL·Docker GUI 전달 경로에서 별도로 확인해야 한다.
