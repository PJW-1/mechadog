# tools/ 색인

이 폴더의 스크립트가 각각 무엇을 위한 것인지 빠르게 찾도록 묶은 색인이다. 파일을 옮기지는 않는다.

표의 두 열은 코드로 확인한 값이다.

- **하드웨어**: 필요(실물 로봇·센서·카메라가 있어야 뜻이 있다) / 불필요(오프라인·파일 기반) / 선택(목업·시뮬레이션·저장된 데이터로도 돌아간다)
- **명령**: 보냄(로봇에 명령을 전송한다) / 읽기전용(로봇에 아무것도 보내지 않는다)

## 로봇 없이 대시보드 보기

```
python tools/mock_mechdog.py --device mechdog-01
python -m host.runtime --device mechdog-01 --robot-ip 127.0.0.1 --no-vision --dashboard-port 8000
```

첫 줄이 가상 로봇을 띄우고, 둘째 줄이 그 가상 로봇을 상대로 호스트 런타임과 관제 대시보드(8000번 포트)를 띄운다. `--no-vision`은 카메라·검출 워커 없이 모션 런타임만 돌린다.

## 운영 도구

| 파일 | 설명 | 하드웨어 | 명령 |
|---|---|---|---|
| [teleop.py](teleop.py) | 키보드(방향키/WASD)로 로봇을 수동 조종한다 | 선택(목업 상대로도 연습 가능) | 보냄 |
| [patrol_run.py](patrol_run.py) | 자율 순찰 루프를 운용한다(라이다→컨트롤러→명령) | 선택(`--simulate`) | 보냄 |
| [ota_update.py](ota_update.py) | 검수된 패키지로 고정 TLS 무선 OTA 갱신을 수행한다 | 필요 | 보냄 |
| [mechdog_command.py](mechdog_command.py) | UDP로 임의의 명령(POSE/사운드 등)을 직접 보내는 범용 CLI | 필요 | 보냄 |
| [fetch_models.py](fetch_models.py) | 모델 가중치를 받고 해시로 검증한다 | 불필요 | 읽기전용 |
| [firmware_env.py](firmware_env.py) | 펌웨어 빌드 환경(버전·파일·gitignore)을 점검만 한다 | 불필요 | 읽기전용 |
| [zone_select.py](zone_select.py) | 저장된 지도 위에서 클릭으로 순찰 구역 좌표를 붙인다 | 불필요 | 읽기전용 |
| [make_badges.py](make_badges.py) | 사원증 ArUco 마커를 인쇄용 이미지로 만든다 | 불필요 | 읽기전용 |
| [lidar_slam.py](lidar_slam.py) | 사람이 옮긴 로봇의 정지 스캔들을 정합해 공간 지도를 만든다 | 선택(`--simulate`) | 읽기전용(명령 소켓을 열지 않는다) |
| [lidar_camera_snapshot.py](lidar_camera_snapshot.py) | 정지 상태에서 라이다 점유격자와 카메라 사진을 한 장 찍는다 | 필요 | 읽기전용 |
| [lidar_camera_overlay.py](lidar_camera_overlay.py) | 저장된 라이다 스캔을 보정된 카메라 프레임에 투영한다(오프라인) | 불필요 | 읽기전용 |
| [lidar_live_map.py](lidar_live_map.py) | 외부에서 측정된 위치 피드로 라이다/카메라 지도를 실시간으로 그린다 | 필요 | 읽기전용(명령을 보내지 않는다) |
| [lidar_map_replay.py](lidar_map_replay.py) | 저장된 위치/스캔/사진 이벤트를 재생해 지도를 만든다(오프라인) | 불필요 | 읽기전용 |
| [lidar_scene_export.py](lidar_scene_export.py) | 저장된 지도를 3D 뷰어용 `scene.json`으로 내보낸다 | 불필요 | 읽기전용 |
| [lidar_scene_view.py](lidar_scene_view.py) | 내보낸 scene을 로컬 웹서버로 띄워 확인한다(`scene_viewer/` 사용) | 불필요 | 읽기전용 |
| [ld19_serial_relay.py](ld19_serial_relay.py) | LD19 라이다를 USB-UART로 PC에 직결해 UDP로 중계한다(중계보드 대역) | 필요(시리얼 포트) | 읽기전용(스캔 중계, 로봇 명령 아님) |

## 하드웨어 없이 개발하기

| 파일 | 설명 | 하드웨어 | 명령 |
|---|---|---|---|
| [mock_mechdog.py](mock_mechdog.py) | 명령을 받고 텔레메트리를 응답하는 가상 로봇, 장애 주입 지원 | 불필요(이 도구 자체가 대역이다) | 읽기전용(명령을 받는 쪽) |
| [mock_lidar.py](mock_lidar.py) | UDP 라이다 스캔 프로토콜을 흉내내는 가상 중계 노드 | 불필요 | 읽기전용 |

## 실기 측정·점검(probe)

| 파일 | 설명 | 하드웨어 | 명령 |
|---|---|---|---|
| [camera_link_check.py](camera_link_check.py) | 카메라 스트림만으로 프레임 도착률·지연을 잰다 | 필요 | 읽기전용 |
| [vision_link_check.py](vision_link_check.py) | 카메라→검출/추적/뱃지 처리까지 걸리는 시간을 잰다 | 필요 | 읽기전용 |
| [latency_probe.py](latency_probe.py) | 촬영→호스트 도착 구간 지연을 화면 카운터로 잰다(`page`/`capture`/`report`/`chain`) | 선택(부속 명령에 따라 카메라 필요) | 읽기전용 |
| [telemetry_probe.py](telemetry_probe.py) | 텔레메트리 수신 주기를 관측한다(수신 전용) | 선택(실물/목업 모두 가능) | 읽기전용 |
| [sensor_log_check.py](sensor_log_check.py) | 이미 저장된 UART 센서 로그를 요약한다(오프라인) | 불필요 | 읽기전용 |
| [udp_probe.py](udp_probe.py) | UDP 프로브를 보내고 에코 응답을 검증한다 | 필요 | 보냄 |
| [motion_probe.py](motion_probe.py) | 정해진 동작을 시키면서 IMU·라이다 실측을 나란히 기록한다 | 필요 | 보냄 |
| [gait_calibrate.py](gait_calibrate.py) | 전진/후진/좌우선회 이동량을 실측해 개체 프로파일에 반영한다 | 선택(`--host 127.0.0.1`로 목업 연습) | 보냄 |
| [g1_acceptance.py](g1_acceptance.py) | G1 게이트 검수(조종·정지·텔레메트리·저전압)를 수행한다 | 필요(받침대에 올려 다리를 띄운다) | 보냄 |
| [obstacle_stop_probe.py](obstacle_stop_probe.py) | 근접 장애물 자동 정지(전진만 거부하는지)를 실기로 확인한다 | 필요 | 보냄 |
| [posture_pitch_probe.py](posture_pitch_probe.py) | 자세별 IMU 피치·롤을 실측한다 | 필요 | 보냄 |
| [posture_escalation_probe.py](posture_escalation_probe.py) | 자세 상승 로직을 카메라+로봇 실기로 확인한다 | 필요 | 보냄 |
| [service_action_probe.py](service_action_probe.py) | SERVICE 모드에서 ACTION이 거부되고 재부팅하지 않는지 확인한다 | 필요 | 보냄(`--regression` 없이는 STOP만) |
| [runtime_compare.py](runtime_compare.py) | 정지 상태 런타임을 캡처하고(`capture`) 오프라인으로 비교한다(`summarize`/`compare`) | 선택(캡처만 실물 필요) | 보냄(캡처 중 STOP·STATE IDLE만) |
| [vlm_bench.py](vlm_bench.py) | VLM 고정 질문 판정을 사진으로 재현율·오경보율·지연으로 잰다(`capture`/`measure`) | 선택(`capture`만 카메라 필요) | 읽기전용(로봇 명령 없음) |
| [lidar_inspect.py](lidar_inspect.py) | LD19 원시 스캔의 회전속도·점수·노이즈 통계를 잰다 | 필요(시리얼 포트) | 읽기전용 |
| [field_measure.py](field_measure.py) | 항목별(0~10) 현장 실측 CLI. 항목 0은 수신만, 구동 항목은 `--allow-motion` 필요 | 선택 | 보냄(선택, `--allow-motion`) |
| [field_plan.py](field_plan.py) | 기체별 실측 계획·기록 GUI 노트북, 항목 실행 시 `field_measure_ui`를 부른다 | 선택(항목 실행 시에만) | 보냄(선택) |
| [field_measure_ui.py](field_measure_ui.py) | `field_plan.py`가 쓰는 논블로킹 측정 진행 창(워커 스레드), 직접 실행하지 않는다 | 선택(field_plan 경유) | 보냄(선택) |
| [field_sessions.py](field_sessions.py) | 항목별 측정 실행 로직 라이브러리, `field_measure_ui`/`run_movement_batch`가 공유한다 | 선택(호출한 도구에 따름) | 보냄(선택) |
| [run_movement_batch.py](run_movement_batch.py) | 이동 테스트 카탈로그를 GUI 없이 터미널에서 일괄 실행한다 | 필요 | 보냄 |
| [pose_recheck.py](pose_recheck.py) | 단일 pitch 자세를 posture 경로와 같은 명령 시퀀스로 재측정한다 | 필요 | 보냄 |

## 개발 보조·CI

| 파일 | 설명 | 하드웨어 | 명령 |
|---|---|---|---|
| [wbs_assignments.py](wbs_assignments.py) | WBS 작업 사전에서 담당자별 작업 목록을 생성한다(`--check`로 대조) | 불필요 | 읽기전용 |
| [check_firmware_scope.py](check_firmware_scope.py) | 카메라·로봇 펌웨어를 한 PR에서 같이 바꾸지 못하게 diff를 검사한다 | 불필요 | 읽기전용 |

## 하위 폴더

- [pc_control/](pc_control/README.md) — Windows 개인 PC용 로봇 유지보수 GUI(Wi-Fi OTA·USB 모드 전환). 자체 README를 둔다.
- `ppe/` — PPE 데이터셋 전처리 스크립트. 개발 중.
- `scene_viewer/` — `lidar_scene_view.py`가 서빙하는 3D 지도 뷰어 정적 파일(HTML/JS/CSS), 단독 실행 대상이 아니다.

## PPE (개발 중)

`ppe/`(전처리 스크립트 4개)와 [ppe_live_check.py](ppe_live_check.py)는 개발 중이다.
