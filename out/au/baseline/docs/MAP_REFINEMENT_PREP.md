# 자동보정 관측 순찰 — PC 사전 준비

실측으로 만든 초기 지도를 바탕으로 정지 관측 지점을 순서대로 방문하고, 새 장애물 앞에서 멈춘 뒤 우회 경로를 다시 계산한다. 도착한 지점에서는 실제 정지·수평 상태를 확인하고 LiDAR 관측을 지도 **후보 사본**에 누적한다. 이 변경은 기존 제어·통신·측위 모델을 바꾸지 않는 PC 모듈과 오프라인 파일 재생 도구다. 로봇 연결, 설치, 펌웨어 배포, 실제 주행은 포함하지 않는다.

## 실행 파일

| 파일 | 동작 |
| --- | --- |
| `host/behavior/refinement_mission.py` | 정본 설정을 읽는 임무 판단. 미관측·점유·제외 구역과 격자 셀 면적을 고려한 여유, A* 셀 경로, 전방 정지·연속 장애물 확인·우회 재계획, 도착 후 정지 관측 |
| `host/slam/map_refinement.py` | 기존 ContinuousMap 재사용. 검증한 센서 중심 위치에서 완전한 관측 회전을 누적하고 지도 사본·내용 revision·관측 ID를 반환 |
| `tools/ops/refinement_prepare.py` | 원본 NPY+메타데이터·요청 실내 범위·관측 동선을 읽어 구간별 경로, 제외 마스크, 임무 후보를 저장 |
| `tools/ops/refinement_replay.py` | 실제 기록 JSONL의 지도 후보 갱신 재생 또는 과거 정지 스냅샷의 적격성 검사 |
| `tools/ops/refinement_session_replay.py` | 임무·지도 갱신·기존 Commander를 파일 sink로 조합. 판단·규약 전문을 저장하며 네트워크 전송은 제공하지 않음 |

저장소 루트에서 `python -m tools.ops.refinement_prepare --help`, `python -m tools.ops.refinement_replay --help`, `python -m tools.ops.refinement_session_replay --help`로 인자를 확인한다. 개인 집 좌표와 센서 원본은 로컬 인자로 전달하며 저장소에 포함하지 않는다.

준비 결과의 `mission.json`은 전체 후보 정지 순서, `mission_primary.json`은 연결된 첫 왕복 루프다. 경로가 막히면 임의 목표 생략으로 성공 처리하지 않는다. `scope.json`의 `allowed_rectangles_m`은 요청 실내 영역이며 그 바깥은 지도 확대 후에도 점유로 유지한다. 실제 센서가 관측했다고 제외 구역을 다시 열지 않는다.

```powershell
python -m tools.ops.refinement_prepare --map-dir "<saved-map>" --stem slam_map --structure "<structure.json>" --route-spec "<route-spec.json>" --out "<separate-candidate-folder>"
python -m tools.ops.refinement_replay --observations "<observations.json>" --out "<empty-audit-folder>"
python -m tools.ops.refinement_session_replay --input "<recorded-events.jsonl>" --map-dir "<saved-map>" --mission "<mission_primary.json>" --frame-id "<verified-map-frame>" --lidar-device "<lidar-unit>" --robot-device "<robot-unit>" --no-go "<scope.json>" --out "<empty-replay-folder>"
```

## 좌표·입력 계약

모든 공간 단위는 m·rad, 수신 시각은 같은 Host 시계의 정수 ms다. 기록은 단조 시각 순서다. 원본 SCAN과 TELEMETRY의 device/boot/seq 검증을 기존 디코더로 수행한다. 잘못된 패킷은 신선도·링크 시각을 갱신하지 않는다.

- `localization`: `pose_origin="base_link"`의 로봇 기준점 위치·body yaw, `frame_id`, `localization_valid`, `laser_extrinsics_verified`, 실제 `stationary`·`level`을 제공한다. 후보 도면 정합은 실제 측위로 승격하지 않는다.
- `pose`: `pose_origin="laser"`, `yaw_frame="base_link"`로 **센서 중심 XY와 body yaw**를 제공한다. SCAN 각도는 정본 mount yaw·angle direction을 디코더에서 한 번만 적용하므로, 센서 장착 yaw를 이 자세에 다시 더하지 않는다. 측정한 base→laser 변환을 producer가 적용한 경우만 외부변환을 검증했다고 표시한다.
- `scan`: `payload`에 원본 SCAN JSON을 보관한다. 과거 median 스냅샷을 여러 패킷으로 복제해서 완전 회전으로 만들지 않는다.
- `telemetry`: `payload`에 원본 TELEMETRY를 보관한다. 온보드 안전 래치와 명령 나이를 확인한다. STOP 송신은 정지·수평 관측을 대신하지 않는다.
- `tick`: 기록된 판단 시각을 제공한다. 모듈은 sleep·소켓·실제 시간을 기다리지 않는다. `localization_lost`는 관측 배치를 즉시 폐기한다.

관측 지점에서 STOP 후 실제 상태가 연속 750ms 이상 안정돼야 한다. 지도 갱신에는 250ms 이내 센서 자세, 500ms 이내 270개 이상 각도 bin, 같은 frame, 검증한 외부변환, 실제 정지·수평이 추가로 필요하다. 중간 측위 상실·불안정·잘못된 관측은 기존 배치를 폐기한다.

새 지도 내용 revision과 관측 ID는 별개다. 정상 재관측으로 셀 내용이 그대로여도 새 관측 ID를 확인하면 다음 지점으로 진행한다. 동일 관측 ID는 두 번 수락하지 않는다. 지도 반영 후 기존 경로를 폐기하고 STOP부터 재계획하며, 격자 확장 때 동적 장애물을 세계 XY에서 재투영한다. 제외 구역은 영구적으로 다시 적용한다.

## 현재의 적용 한계

CLI는 후보 지도·후보 임무를 실제 주행에 활성화하지 않는다. 세션 재생의 `frame_verified` 기본값은 false다. 미관측 공간을 채우거나 막힌 지점을 자동으로 뛰어넘어 임무가 끝난 것처럼 처리하지 않는다.

큰 방향 오차는 `pending_inplace_turn_driver`로 정지한다. 기존 별도 제자리 회전 변경과 실시간 외부 측위 운용의 호환성을 확인한 뒤 실제 driver에 연결해야 한다. 파일에 생성한 MOVE/STOP/ESTOP는 오프라인 검증 결과이며 전송 코드가 아니다. 기존 온보드 정지와 사용자 해제 절차는 유지된다.

LiDAR 한 평면은 식탁 다리·가구의 한 단면을 본다. 통과 가능한 셀만으로 가구 전체 크기나 로봇 상부 통과 공간을 확정하지 않는다. 사진·RTAB과 객체 대응, 실제 로봇 외곽, map→집 좌표 정합을 확인한 뒤 SIM 기하 수정 후보를 검토한다. 임의 가구 축소·삭제와 SIM 정본 자동 덮어쓰기는 제공하지 않는다.

관련 WBS 5.4.3~5.4.5의 실제 odom/tf, 연속 측위, 실물 순찰 완료 조건은 별도 실측 검증 전까지 미완료다.
