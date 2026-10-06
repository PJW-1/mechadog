# LiDAR 지도에 카메라 사진 연결

`tools/lidar/lidar_slam.py`의 기존 정지 스캔 지도에, 같은 위치에서 PC가 받은 카메라 JPEG를
연결하는 선택 기능이다. 카메라와 라이다를 **동시에 수신**하지만, JPEG는 지도 위의
물체 위치가 아니라 **스캔을 정합한 센서 위치**에 붙는다. 보정되지 않은 카메라의
검출 박스를 실좌표라고 표시하지 않는다.

## 사용 전제

- 로봇 구동 명령은 보내지 않는다. 기존 지도 도구처럼 각 측정 위치에서 완전히
  정지한 뒤 스캔을 받는다. 한 자리에서는 그 자리에서 보이는 영역만 채운다.
- 라이다 장착 각도(`mount_yaw_deg`, `angle_direction`)가 해당 기체에서 실측되어
  있어야 한다. `mechdog-02`의 2026-09-26 방위 수정은 현재 별도 로컬 브랜치에
  있으며 운영 `dev` 설치 여부와 구별해야 한다.
- 카메라 주소는 현장에서 확인한 `http://<카메라IP>:81/stream`을 명시한다.
  프로파일의 `xiao_ip`가 비어 있어 추측하지 않는다.
- 다른 프로그램이 UDP 5201을 소유 중이면 포트 충돌 오류를 확인하고 한 수신기만
  사용한다. 이 도구는 소켓 재사용으로 충돌을 숨기지 않는다.

## 여러 정지 위치에서 실행

```text
python tools/lidar/lidar_slam.py --device mechdog-02 --lidar-device lidar-b03fd35ee950 --camera-url http://<실측한카메라IP>:81/stream --steps 3 --out maps/paired-run
```

지도 작업은 기존 `localization.track: lidar` 설정이 요구된다. URL을 주지 않으면
이전과 동일한 라이다 단독 동작이다. 카메라가 끊겨도 라이다 지도가 계속 저장되고,
오래된 사진은 지도에 붙이지 않는다. 각 스캔 배치가 끝난 PC 시각과 JPEG가 도착한
PC 시각의 차이가 500ms 이내이고 JPEG가 정상 형식인 경우만 저장한다.
**실제 촬영 시각은 JPEG에 없으므로 이 차이를 촬영 동기 오차로 해석하지 않는다.**

산출물은 `slam_map.npy`/`map_meta.json`/`slam_map.pgm`/`slam_map.yaml`과
`photo_poses.json`, `photos/step-XXXX.jpg`, `photo_map.html`이다. HTML의 번호를
누르면 그 위치의 사진이 열린다. 회색 영역은 미관측이다. `matplotlib`가 없는
환경에서는 HTML 대신 JSON과 JPEG만 남는다.
사진 지도 실행의 `--out` 폴더는 새 폴더(또는 빈 폴더)여야 한다. 이전 측정 사진과
새 지도가 섞이면 잘못된 위치로 보일 수 있으므로 기존 산출물을 덮어쓰지 않는다.

## 2026-09-27 정지 수신 확인

`tools/lidar/lidar_camera_snapshot.py`는 로봇을 움직이거나 명령을 보내지 않고 현재
위치에서 LiDAR 스캔과 XIAO MJPEG 한 장을 기록한다.

```powershell
python tools/lidar/lidar_camera_snapshot.py --device mechdog-02 --lidar-device lidar-b03fd35ee950 --camera-url http://192.168.0.19:81/stream --seconds 5 --out maps/stationary-01
```

출력: 원본 `scans.jsonl`, 점유격자 `static_map.npy` + `map_meta.json`, ROS2용
`static_map.pgm` + `static_map.yaml`, 보기용 `static_map.png`, 사진과 촬영 위치를
보여주는 `photo_map.html`, `summary.json`. 출력 폴더는 비어 있어야 한다.
5초 창에서 유효 스캔 50개·각도 270칸 이상과 신선한 JPEG가 없으면 실패한다.

이 지도는 **단일 자세 관측**이다. 회색은 비어 있는 공간이 아니라 미관측이다.
카메라 핀은 로봇 기준 원점의 촬영 위치만 뜻한다. 사진 속 물체의 위치는
카메라 내부·외부 보정과 두 센서의 실제 동시 시야가 확인될 때까지 추정하지
않는다. 전체 방 지도는 여러 자세의 관측과 정합이 별도로 필요하다.

Windows에서 UDP5201을 확인할 때 PowerShell 수신이 앱 방화벽 정책에 막힌
사례가 있다. 이 저장소의 Python 수신 경로로 패킷을 확인한다. 이 도구는
`SO_REUSEADDR`를 쓰지 않으므로 다른 수신기가 포트를 점유하면 즉시 실패한다.

## 여러 위치의 스캔 누적·재생

`tools/lidar/lidar_map_replay.py`는 시간순 자세·LiDAR·JPEG 이벤트를 받아 지도를
누적하고 `map_view.html`에 로봇 경로와 지도 갱신 수를 표시한다. `--input -`로
표준 입력의 연속 이벤트를 받을 수 있으며, 지도는 기본 5회 반영마다 갱신된다.
이 도구는 로봇에 명령을 보내지 않는다.

```powershell
python tools/lidar/lidar_map_replay.py --simulate --out maps/sim-continuous-01
python tools/lidar/lidar_map_replay.py --device mechdog-02 --lidar-device lidar-b03fd35ee950 --input events.jsonl --camera-url http://192.168.0.19:81/stream --out maps/real-replay-01
```

입력은 각 줄에 JSON 객체 하나다. `received_ms`는 **PC에서 받은 epoch ms**이고
시간순이어야 한다. 종류별 예시는 아래와 같다.

```json
{"kind":"pose","received_ms":1000,"x_m":0.0,"y_m":0.0,"yaw_rad":0.0,"stationary":true,"level":true}
{"kind":"photo","received_ms":1010,"path":"photos/frame-001.jpg","seq":1}
{"kind":"scan","received_ms":1020,"payload":{"seq":1,"ts":12345,"type":"SCAN","device_id":"lidar-b03fd35ee950","boot_id":"boot","points":[[270,623]]}}
```

`scan.payload`는 기존 LiDAR UDP JSON 전문 그대로다. `photo.path`는 입력 파일을
기준으로 상대 경로 또는 절대 경로다. 실제 SCAN은 한 UDP에 일부 각도만 오므로
여러 패킷이 270개 이상의 유효 각도를 채운 뒤 한 바퀴로 반영한다. 자세 입력이
250ms 넘게 오래됐거나 없으면 지도를 갱신하지 않는다. `stationary`·`level`이
거짓이면 위치 경로만 기록하고, 스캔은 영구 지도에 적산하지 않는다. 이 플래그는
실제 위치·IMU 판정에서 공급해야 한다. 같은 묶음 내 위치 2cm·방위 2° 초과도
지도 반영을 막는다.

현재 `--simulate`는 가상 공간의 **알고 있는 6개 위치**를 공급해 지도 표시만
검증한다. 실물 이동 경로를 직접 공급하는 WBS 5.4.3 오도메트리/tf와
5.4.4 ROS2 지도 연결은 별도 작업이며, 명령값만을 실제 위치로 위장해
지도 합격으로 표시하지 않는다. 카메라 사진은 위치 핀이지 검출 물체 좌표가 아니다.

### 실시간 수신 경로

`tools/lidar/lidar_live_map.py`는 LiDAR UDP 5201, XIAO MJPEG 및 **PC 루프백에만
바인드한 자세 입력 5202**를 동시에 읽고 5회 지도 갱신마다 `map_view.html`을
다시 쓴다. 로봇 명령 소켓은 열지 않는다.

```powershell
python tools/lidar/lidar_live_map.py --device mechdog-02 --lidar-device lidar-b03fd35ee950 --camera-url http://192.168.0.19:81/stream --seconds 60 --out maps/live-01
```

자세 공급자가 루프백 UDP 5202로 보내야 할 값은 아래와 같다. 도착 시각은 수신
PC가 찍으며, 로봇의 부팅 후 `ts`를 epoch로 쓰지 않는다.

```json
{"x_m":0.0,"y_m":0.0,"yaw_rad":0.0,"stationary":true,"level":true}
```

**현재 이 자세 공급자는 아직 연결되지 않았다.** `mechdog-02`의 보행 속도
실측값만으로 임의의 제어 명령을 정확한 위치로 만들 수 없다. 따라서 현재
실물에서 도구를 실행해도 자세 입력이 없으면 LiDAR 패킷 수만 기록하고
지도를 갱신하지 않는다. `stationary`·`level`은 실제 정지·IMU 판정에서
생성해야 하며, 시험용 상수 `true`를 보내 실물 지도 성공이라 할 수 없다.
기존 Windows 수신기와 이 도구를 동시에 UDP 5201에 바인드하지 않는다.

## sim 장면 데이터로 내보내기 (라이다 선 분리 상태에서도 가능)

저장된 지도 폴더의 `slam_map.npy`/`map_meta.json`을 읽어 `scene.json`을 만든다.
실기 센서 연결이나 로봇 명령 없이 실행할 수 있다.

```powershell
python tools/lidar/lidar_scene_export.py --device mechdog-02 --map maps/real-replay-01 --out maps/real-replay-01/scene.json
```

`scene.json`은 실측 XY 좌표 기준의 점유/빈 공간 **행 구간**, 미관측 셀,
라이다 중심 경로, 카메라 촬영 위치와 사진 참조를 담는다. 사진을 벽의 질감으로
붙이거나 사진 속 물체를 3D 좌표로 가장하지 않는다. 현재 `navigation_ready=false`,
높이는 `null`이다. 이 파일은 sim 렌더러가 실제 측정과 미측정을 구별해서 읽기
위한 데이터 계약이며 **실사형 집 모델이나 실제 자율주행 완성품은 아니다.**
그 단계에는 실측 위치·센서 좌표 변환, 카메라 내부/외부 보정, 다시점 3D 재구성,
현실 주행 시험이 남아 있다. PC↔데브킷 USB는 별도 전원과 Wi-Fi 중계가
확인되면 필요 없지만, LiDAR↔데브킷의 전원/UART 배선은 실측 때 연결한다.

### PC에서 3D 지도 미리보기

저장된 `scene.json`을 읽는 독립 뷰어다. 운영 대시보드나 로봇 명령 경로와
연결되지 않는다. 로컬 서버는 `127.0.0.1`에만 열리며 지도, 뷰어 자산,
해당 폴더의 JPEG만 읽기 전용으로 제공한다.

```powershell
python tools/lidar/lidar_scene_view.py --map maps/real-replay-01 --port 8765
```

브라우저에서 `http://127.0.0.1:8765/`을 열면 관측된 빈 공간·장애물 단면·
미관측 영역·이동 경로·카메라 촬영 위치를 볼 수 있다. 위/입체 시점을 바꿀 수
있고 카메라 지점을 누르면 사진을 옆 패널에서 확인한다. 세로로 보이는 장애물은
**높이가 측정된 벽이 아니라 평면 스캔을 구별하기 위한 얇은 기호**다.

기존 단일 위치 실물 기록(`static_map.npy`)도 아래처럼 내보낼 수 있다.

```powershell
python tools/lidar/lidar_scene_export.py --device mechdog-02 --map <실물_기록_폴더> --stem static_map --out <새_미리보기_폴더>/scene.json
python tools/lidar/lidar_scene_view.py --map <새_미리보기_폴더> --port 8765
```

사진을 함께 보려면 해당 미리보기 폴더의 `photos/`에 원본 사본을 두고 사본
해시를 확인한다. 원본 실측 폴더를 덮어쓰지 않는다. 단일 위치 스캔은 방
전체의 닫힌 지도나 검증된 경로가 아니며, 이 뷰어에서 현실 주행은 할 수 없다.

## 카메라·라이다 기하 보정 (기체02, 2026-09-27 잠정 실측)

사용자 실측: 카메라 렌즈 중심은 바닥 위 약 **14cm**이고 라이다 중심보다
**전방 10cm**, 좌우 편차 없음(위에서 본 수평 거리). 카메라 광축은 수평보다
**위로 15°**를 향한다. 아래 계산 모듈의 `pitch_down_deg`는 아래가 양수이므로
입력은 **−15**다. 라이다 회전 원통 중간이 바닥 위 약 **22.5cm**이나, 이것이
실제 레이저 **스캔면 높이**와 같은지는 확인 전이다. 원통 외형의 중간 대신
레이저가 빠져나가는 광학창의 중심 높이를 바닥에서 재고 같은 자세에서 기록한다.
장착 차이와 수평 여부 때문에 높이 오차가 곧 투영 오차가 된다.

`host/slam/camera_lidar_geometry.py`는 라이다 중심을 로봇 XY 원점으로 삼아
두 계산을 한다: (1) **바닥이라고 확인된** 화면 픽셀의 바닥 좌표, (2) 라이다가
잰 단일 높이의 XY 반사점을 카메라 픽셀로 투영. 화면 물체 윗점과 바닥 접점이
같은 수직 물체라고 검증된 경우에만 대략 높이를 계산하고 수평 불일치도 함께
돌려준다. 바닥 접점이 가려졌거나 광선이 수평선 위면 거리·높이를 내지 않는다.
카메라가 위로 향하므로 **화면 중앙은 바닥을 보지 않는다**. 가시 바닥은 화면
하단 일부일 수 있다. 2D 라이다 한 줄만으로는 물체 꼭대기나 벽 높이를 잴 수 없다.

`tools/lidar/lidar_camera_overlay.py`는 저장된 `scans.jsonl`과 같은 자세의 정방향
사진에 라이다 **스캔면** 반사점만 표시한다. `--calibration` JSON에는
`camera.width_px/height_px`, `fx_px/fy_px/cx_px/cy_px`, `height_m=0.14`,
`forward_m=0.10`, `left_m=0`, `pitch_down_deg=-15`, `yaw_left_deg`와
`lidar_height_m`, `mount_yaw_deg=270`, `angle_direction=-1`을 명시해야 한다.
여기서 높이·오프셋·각도는 잠정값이고 `fx/fy/cx/cy`, 카메라 실제 yaw 및
라이다 스캔면 높이는 **현재 장착 상태에서 아직 검증되지 않았다**. 09-13의
`f≈424px`는 과거 장착 화각 자료이지 현행 교차 보정의 합격 근거가 아니다.
사진이 180° 회전 상태로 저장됐다면 먼저 영상 좌표와 캘리브레이션 방향을
일치시킨다. 도구는 필수 보정값이 없으면 실패하며 결과를 실제 3D 물체로
해석하지 않는다. 출력은 새 폴더의 `lidar_scan_plane_overlay.png`와
`projection.json`이다. 기존 사진/스캔과 운영 지도는 변경하지 않는다.

```powershell
python tools/lidar/lidar_camera_overlay.py --photo <같은-자세-사진.jpg> --scans <scans.jsonl> --calibration <실측-calibration.json> --out <새-출력-폴더>
```

현 저장된 `stationary_map_01` 사진에는 PC 모니터가 크게 찍혀 있어 라이다
벽점과 이미지 벽점을 대조할 수 없다. 다음 실측은 정지 상태에서 공통 시야에
바닥·벽 경계와 높이를 아는 표식을 두고, 줄자로 거리를 기록한 사진과 스캔을
같이 확보해야 한다. 이 잔차 확인 전에는 오버레이 점을 검출 물체의 확정 위치나
자율주행용 장애물로 쓰지 않는다.
