"""SLAM 코어 — 스캔을 넣으면 **맵과 위치**를 돌려준다 (FR-6 · Phase 2).

    LiDAR ─UART─▶ ESP32 DevKit ─UDP(SCAN)─▶ [ 여기 ] ─▶ 맵 + 자세
                                              │
                     behavior/planner · zones · patrol 가 그것을 소비한다

`WBS 3.9.0` **"SLAM 코어"**(구역 앵커 `3.9.1` 의 선행)가 이 패키지다.

---

## 이 패키지의 절반은 `slam_toolbox` 로 교체될 자리다

[ADR-9](../../docs/DECISIONS.md) 가 정한 것 —

> Phase 1 을 **순수 Python 단일 프로세스로 완결**. ROS2 는 Phase 2 에서
> `slam_toolbox` 하나로 범위를 한정해 *"스캔을 넣으면 맵과 위치를 반환하는
> **블랙박스**"* 로만 쓴다.

그래서 모듈이 **교체될 것과 남을 것으로 나뉘어 있다.**

| 모듈 | P2 에서 | 이유 |
| :--- | :--- | :--- |
| `scan_match.py` | **`slam_toolbox` 로 교체** | 정확히 그 블랙박스가 하는 일이다 |
| `occupancy.py` | **남는다** | 맵을 *소비*하는 쪽이다. `slam_toolbox` 의 `*.pgm`+`*.yaml` 을 읽는다 |
| `settings.py` · `simulation.py` · `viz.py` | 남는다 | 설정 조립 · 개발 도구 |

**경로계획은 교체 대상이 아니다.** ADR-9 가 **nav2 를 쓰지 않기로** 함께
정했으므로 A*(`behavior/planner.py`)와 순찰 스케줄러(`behavior/zones.py`)는
측위 방식과 무관하게 남는다.

## 교체가 성립하려면 지켜야 하는 것 하나

**`OccupancyGrid.load()` 가 ROS2 맵을 읽을 수 있어야 한다.** `slam_toolbox` 가
낸 지도로 구역 지정과 순찰이 **코드 변경 없이** 돌아야 블랙박스 교체가 성립한다.
`test_load_falls_back_to_the_ros2_pair` 가 그 계약이다. 그 판독은 미지 영역을 자유로
만들지 않는다 (`occupancy.load_ros2`).

## ROS2 쪽 구성 — Phase 2

| 무엇 | 어디 |
| :--- | :--- |
| `ros:jazzy` + `slam_toolbox` + `rviz2` 컨테이너 | `docker/ros2/` (WBS 5.4.1) |
| `SCAN` → `sensor_msgs/LaserScan` 브리지 | `docker/ros2/scan_bridge.py` · [PROTOCOL_LIDAR 6절](../../docs/PROTOCOL_LIDAR.md) |
| tf `odom` → `base_link` | `host/slam/odometry.py` → `host/common/odom_link.py` → `docker/ros2/odom_bridge.py` (WBS 5.4.3) |

---

단위는 전선 위 mm·deg·cm·ms(PROTOCOL.md 2절), 이 패키지 안 m·rad·s 이고 변환은
`host/common/units.py` 한 곳에서만 한다. `config.localization.track` 이 `lidar` 가 아니면
실기 모드로 기동하지 않는다 (Phase 1 표준 구성에는 LiDAR 가 없다).
"""
