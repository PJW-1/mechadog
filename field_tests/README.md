# field_tests 색인

이 폴더는 실기 시험의 원자료다. `results/` 아래에 시험 한 번당 폴더 하나씩 쌓인다.

폴더 이름 규칙: `YYYYMMDD[-시각]_<작업번호>-<설명>`(예: `20260917_3.2.6-obstacle-stop`). 시각이 없는
이름은 하루에 한 번만 돈 시험이다. `<작업번호>` 는 WBS 항목이거나, WBS 번호가 없는 임시 조사일 때는
주제 이름만 붙는다.

## 실측 기록이 나뉘는 세 곳

- `field_tests/results/<세션>/` (여기) — 시험 한 번의 원자료 그대로다. 로그·콘솔 출력·JSON·이미지를
  가공하지 않고 남긴다.
- [docs/measurements/](../docs/measurements/) — 위 원자료를 사람이 읽게 정리한 글이다. 해석·절차·
  정정 경위가 들어간다.
- [docs/field_measure_catalog.json](../docs/field_measure_catalog.json) — 기계가 읽는 실측 카탈로그다.
  `tools/field_plan.py`(측정 GUI/체크리스트 내보내기)가 이 파일을 읽어 항목별 절차·판정 기준·기존
  근거 URL을 보여준다. 카탈로그는 사람이 직접 채우며, 이 폴더나 `docs/measurements/`를 자동으로
  긁어 만들지 않는다.

`results/` 바로 아래의 루트 파일 둘도 원자료다.

- [stability_2_3_2.csv](results/stability_2_3_2.csv) — WBS 2.3.2 안정성 실측, 30초 간격 장시간 폴링 원자료(응답시간·RSSI 등)
- [vision_bench.json](results/vision_bench.json) — WBS 3.3.x 비전 파이프라인 벤치마크 원자료(추론 지연·프레임 드롭률 등)

## 대표 기록

| 무엇을 증명하는가 | 핵심 결과 | 폴더 |
| :--- | :--- | :--- |
| G1 게이트(조종·정지·텔레메트리·저전압) 4개 기준을 기준기가 충족하는가 | **4항목 전부 통과** — 송신 중단 후 IMU 0.38°로 정지하며 SAFE 래치, 텔레메트리 30초 300건(10.00Hz), 저전압 페일세이프 `RESET_SAFE` 거부 6/6 (2026-09-17 · `mechdog-01`) | [20260917_6.4.1-g1](results/20260917_6.4.1-g1/summary.md) |
| 온보드 근거리 반사 정지가 전진만 거부하고 후진 탈출로는 열려 있는가 | **9/9 통과.** 다만 초음파가 21cm 표적을 13%꼴로 34cm 로 잘못 읽어(150표본 중 20건) 해제 2표본에서는 회피 상태가 29회 깜빡였다 — 해제를 5표본으로 올려 0회로 고쳤다 (2026-09-17 · `mechdog-01`) | [20260917_3.2.6-obstacle-stop](results/20260917_3.2.6-obstacle-stop/summary.md) |
| 양방향 조향 보정을 걸면 좌우 선회가 대칭이 되는가 | **기각.** 11.5배 비대칭이 나와 되돌렸다 (2026-09-19 · `mechdog-01`) | [20260919_bias-symmetry](results/20260919_bias-symmetry/summary.md) |
| 조향 각도별 선회율(순 회전) 곡선을 실측한다 | +28°에서 +9.41°/s, −28°에서 −7.90°/s 로 좌우가 비대칭이었고, 자이로 바이어스가 부팅마다 달라(세 부팅에서 +38.4 / +0.2 / −1.1 °/s) 정지 기준선을 같은 부팅에서 빼야 함을 확인했다 (2026-09-18 · `mechdog-01`) | [20260918_turn-curve](results/20260918_turn-curve/summary.md) |
| 검출→명령 적용까지 지연 체인의 각 구간 시각을 프레임 단위로 남긴다(NFR-1.1) | 이 폴더는 `summary.md`가 없다 — 통과/기각을 단정하지 않는다. `chain.json`에 프레임별 `decode_infer_ms`(9~11ms)·`ack_ms_from_detect`(1~7ms)만 원자료로 남아 있다 (2026-09-15 · `mechdog-01`) | [20260915_nfr1.1-chain](results/20260915_nfr1.1-chain/chain.json) |

## 전체 목록

PPE 관련 폴더(이름에 `ppe`가 들어가거나 `3.7.x`)는 개발 중이라 결과를 적지 않는다.

| 폴더 | 날짜 | 주제 | 결과 |
| :--- | :--- | :--- | :--- |
| [20260912-000123](results/20260912-000123/report.md) | 2026-09-12 | 로봇+카메라 네트워크·안전 초기 통합 점검 | 통과 12·건너뜀 2(서보·텔레메트리 미구현) |
| [20260912-023331](results/20260912-023331/report.md) | 2026-09-12 | 〃(재점검) | 통과 10·실패 1·건너뜀 1 |
| [20260912-023654](results/20260912-023654/report.md) | 2026-09-12 | 〃(재점검) | 통과 2·실패 1·건너뜀 9 |
| [20260912-024102](results/20260912-024102/report.md) | 2026-09-12 | 〃(재점검) | 통과 12·실패 1·건너뜀 1 |
| [20260912-024152](results/20260912-024152/report.md) | 2026-09-12 | 〃(재점검) | 통과 12·실패 1·건너뜀 1 |
| [20260914-175258_4.5.1-dashboard](results/20260914-175258_4.5.1-dashboard/summary.md) | 2026-09-14 | 관제 WebSocket 서버(다중 클라이언트·10Hz) | 통과 |
| [20260914-185200_4.5.2-vision-overlay](results/20260914-185200_4.5.2-vision-overlay/vision_probe.txt) | 2026-09-14 | 관제 화면 검출 오버레이 확인 | 통과(카메라 자세 문제 발견 후 재확인) |
| [20260914_4.6.3-joystick](results/20260914_4.6.3-joystick/summary.md) | 2026-09-14 | 관제 화면 수동 조작·E-Stop | 통과(결함 3건 발견·같은 날 수정) |
| [20260915_2.1.4-posture-pitch](results/20260915_2.1.4-posture-pitch/summary.md) | 2026-09-15 | 자세별 IMU 몸통 피치 실측 | 기록만 |
| [20260915_4.1.2-pose-action](results/20260915_4.1.2-pose-action/summary.md) | 2026-09-15 | HAL 에 POSE·ACTION 붙이기 | 통과(결함 3건 발견·같은 PR 에서 수정) |
| [20260915_4.4.3-event-feed](results/20260915_4.4.3-event-feed/summary.md) | 2026-09-15 | 이벤트 블랙박스·관제 푸시 | 통과 |
| [20260915_4.6.2-telemetry-gauges](results/20260915_4.6.2-telemetry-gauges/summary.md) | 2026-09-15 | 관제 화면 상태 게이지·추이 표시 | 기록만 |
| [20260915_4.6.4-event-feed](results/20260915_4.6.4-event-feed/summary.md) | 2026-09-15 | 사건 피드·스냅샷 표시 | 통과 |
| [20260915_nfr1.1-chain](results/20260915_nfr1.1-chain/chain.json) | 2026-09-15 | 검출→명령 적용 지연 체인 원자료 | 기록만(요약 없음) |
| [20260916_2.1.3-sonar](results/20260916_2.1.3-sonar/summary.md) | 2026-09-15~16 | 초음파 센서 실측 | 결함 발견·수정 확인 |
| [20260917_3.2.6-obstacle-stop](results/20260917_3.2.6-obstacle-stop/summary.md) | 2026-09-17 | 온보드 근거리 반사 정지 | 통과(해제 임계 결함 발견·수정) |
| [20260917_6.4.1-g1](results/20260917_6.4.1-g1/summary.md) | 2026-09-17 | G1 제어 링크 게이트 검수 | 통과 |
| [20260918_3.5.4-recheck](results/20260918_3.5.4-recheck/summary.md) | 2026-09-18 | TRACK 락온 좌우 대칭 재검증 | 통과(방향별 보정으로 좌우 수렴) |
| [20260918_3.5.4-track](results/20260918_3.5.4-track/summary.md) | 2026-09-18 | TRACK 락온 실기 | 부분 실패(오른쪽 수렴 결함, recheck 에서 해결) |
| [20260918_3.7.2-ppe-model](results/20260918_3.7.2-ppe-model/report.md) | 2026-09-18 | PPE (개발 중) | — |
| [20260918_4.7.3-eye-led](results/20260918_4.7.3-eye-led/report.md) | 2026-09-18 | 눈 LED 상태 표시(진단 스케치) | 통과 |
| [20260918_posture](results/20260918_posture/summary.md) | 2026-09-18 | SCAN·ALERT 자세 실기 | 통과 |
| [20260918_turn-curve](results/20260918_turn-curve/summary.md) | 2026-09-18 | 선회율 곡선 실측 | 기록만 |
| [20260919_4.7.3-eye-led-host](results/20260919_4.7.3-eye-led-host/summary.md) | 2026-09-19 | 눈 LED 호스트 발신 경로 실기 | 통과(발신 누락 발견·수정) |
| [20260919_bias-symmetry](results/20260919_bias-symmetry/summary.md) | 2026-09-19 | 양방향 보정·coast·사람 게이트 | 기각(양방향 보정), 나머지 항목 통과 |
| [20260919_ppe-pc-test](results/20260919_ppe-pc-test/summary.md) | 2026-09-19 | PPE (개발 중) | — |
| [20260920_3.5.7-3.5.8](results/20260920_3.5.7-3.5.8/summary.md) | 2026-09-20 | 자세 상승(3.5.7)·거리 유지(3.5.8) 실측 | 결함 발견(IMU 가 카메라 피치 변화를 못 읽음) |
| [20260920_4.8.0-vlm-compare](results/20260920_4.8.0-vlm-compare/summary.md) | 2026-09-20 | VLM 두 장 비교 가능성 | 기각 |
| [20260920_ppe-novest-orient](results/20260920_ppe-novest-orient/summary.md) | 2026-09-20 | PPE (개발 중) | — |
| [20260922_4.7.7-guard-voice](results/20260922_4.7.7-guard-voice/summary.md) | 2026-09-22 | 경비모드 음성 신원 확인 | 통과 |
| [20260922_alarm-web-release](results/20260922_alarm-web-release/summary.md) | 2026-09-22 | 헤드리스 L3 해제·판정 대기 유예 | 통과 |
| [20260922_guard-auth](results/20260922_guard-auth/summary.md) | 2026-09-22 | 경비 모드 휴대폰 ArUco 인증 | 기록만(재인증 반복 원인 미확정) |
| [20260923_4.7.10-robot-status-voice](results/20260923_4.7.10-robot-status-voice/summary.md) | 2026-09-23 | 로봇 상태 음성 응답 | 통과 |
| [20260923_4.7.19-xiao-input](results/20260923_4.7.19-xiao-input/summary.md) | 2026-09-23 | XIAO 음성 입력 전송층 | 통과 |
| [20260923_4.7.9-bridge-transparency](results/20260923_4.7.9-bridge-transparency/summary.md) | 2026-09-23 | 로봇 탑재 오디오 중계(I²C 브리지) | 기각(음성 파형 중계 불가) |
| [20260923_patrol-engage](results/20260923_patrol-engage/summary.md) | 2026-09-23 | 경비 대응 재설계(정지→조준→L1) | 통과(결함 1건 발견·수정) |
| [20260923_person-down](results/20260923_person-down/summary.md) | 2026-09-23 | 쓰러짐 규칙 판정 | 결함 발견(확정 이벤트가 런타임에 덜 도달) |
| [20260923_xiao-mic](results/20260923_xiao-mic/summary.md) | 2026-09-23 | XIAO 마이크 인식 품질·영상 동시 전송 | 통과 |
| [20260924_4.7.20-mp3-driver](results/20260924_4.7.20-mp3-driver/summary.md) | 2026-09-24 | 로봇 MP3 모듈 드라이버 | 통과 |
| [20260924_4.7.21-tf-card](results/20260924_4.7.21-tf-card/summary.md) | 2026-09-24 | TF 카드 문장·로봇 스피커 재생 | 통과 |
| [20260928_4.8.0-vlm-bench](results/20260928_4.8.0-vlm-bench/summary.md) | 2026-09-28 | VLM 판독 카메라 벤치 | 기록만 |
