# 문서 색인

이 저장소의 문서는 독자에 따라 세 갈래로 나뉜다. 처음 본다면 **시스템 이해**부터 읽는다.

## 시스템 이해 (외부 독자용)

무엇을 만들었고 왜 이렇게 만들었는지 알고 싶다면 여기부터 본다.

- [code-tour.md](code-tour.md) — 코드를 15분 안에 훑어보는 읽기 순서와 전체 데이터 흐름이다.
- [ARCHITECTURE.md](ARCHITECTURE.md) — 시스템 구조·상태 머신·품질 기준과 용어를 정리한다.
- [design-decisions.md](design-decisions.md) — 시스템의 모양을 정한 대표 설계 결정을 골라 요약한다.
- [DECISIONS.md](DECISIONS.md) — 검토했으나 채택하지 않은 대안과 그 근거를 남긴다.
- [PROTOCOL.md](PROTOCOL.md) — Host와 로봇 펌웨어가 주고받는 통신 메시지의 정본이다.
- [PROTOCOL_LIDAR.md](PROTOCOL_LIDAR.md) — LiDAR 중계 노드 링크를 위한 PROTOCOL 의 확장 규약이다.
- [DASHBOARD.md](DASHBOARD.md) — 관제 서버와 FPV 화면이 텔레메트리·FSM 상태를 내보내는 방식을 설명한다.
- [ENGINEERING_GUIDE.md](ENGINEERING_GUIDE.md) — 로깅·테스트·CI 를 짤 때 따르는 구현 기준이다.

## 기능별 동작 흐름

로봇이 어떤 조건에서 무엇을 하는지를 기능마다 판단 흐름도(mermaid)와 판단 기준 표, 코드·테스트 대응표로 적는다. 문서가 아니라 코드를 보고 그렸다.

- [features/failsafe.md](features/failsafe.md) — 제어 링크와 페일세이프. 명령 타임아웃·링크 두절·저전압 래치와 해제 조건.
- [features/patrol-obstacle.md](features/patrol-obstacle.md) — 순찰 중 장애물 대응. 반사 정지 뒤 회피와 재시도 상한.
- [features/vision-stream.md](features/vision-stream.md) — 카메라 스트림 수신과 추론 워커. 재연결·최신 프레임 우선.
- [features/vlm-reading.md](features/vlm-reading.md) — VLM 단일 장면 판독의 비동기 처리.
- [features/person-tracking.md](features/person-tracking.md) — 사람 확인·추적. 고정 시간 창 판정과 락온.
- [features/guard-auth.md](features/guard-auth.md) — 경비 모드 인증. 암구호·사원증과 인증 창.
- [features/escalation.md](features/escalation.md) — 대응 에스컬레이션 L0~L3·F 의 상승·하강 조건.
- [features/factory-fall.md](features/factory-fall.md) — 공장 모드 쓰러짐 의심과 확정.
- [features/factory-demo-scenario.md](features/factory-demo-scenario.md) — 공장 모드 시연 한 바퀴(A→B→C→D)의 사건·설정·코드 위치.

## 사례 연구

실기에서 드러난 문제를 증상 → 가설과 배제 → 측정 → 원인 → 수정 → 검증 → 남은 한계 순서로 정리한다. 5~10번은 초기 가설 → 관측된 실패 → 근거 데이터 → 원인 → 변경 → 동일 조건 재평가 순서다. 목록은 [case-studies/README.md](case-studies/README.md) 에 있다.

- [case-studies/01-command-timeout.md](case-studies/01-command-timeout.md) — 명령 타임아웃 300ms → 600ms.
- [case-studies/02-person-gate-time-window.md](case-studies/02-person-gate-time-window.md) — 10fps 추론이 진짜 검출의 절반 이상을 놓친 문제.
- [case-studies/03-echoed-state-self-lock.md](case-studies/03-echoed-state-self-lock.md) — 반향된 상태값 때문에 Host 가 스스로를 잠글 수 있었던 문제.
- [case-studies/04-i2c-voice-relay.md](case-studies/04-i2c-voice-relay.md) — 로봇 I²C 경유 음성 파형 중계가 불가능했던 사례.
- [case-studies/05-ppe-domain-gap.md](case-studies/05-ppe-domain-gap.md) — 공개 PPE 데이터 성능이 XIAO 에서 재현되지 않은 사례.
- [case-studies/06-partial-label-background.md](case-studies/06-partial-label-background.md) — 부분 라벨이 맨머리를 배경으로 가르친 사례.
- [case-studies/07-track-turn-asymmetry.md](case-studies/07-track-turn-asymmetry.md) — TRACK 좌우 비대칭을 회전율 곡선으로 고친 사례.
- [case-studies/08-vlm-two-image-compare.md](case-studies/08-vlm-two-image-compare.md) — VLM 두 장 비교 실패와 한 장 판독 한정.
- [case-studies/09-bbox-height-stop-line.md](case-studies/09-bbox-height-stop-line.md) — bbox 높이 정지선이 화면 밖이었던 사례.
- [case-studies/10-model-provider-mismatch.md](case-studies/10-model-provider-mismatch.md) — 추론 환경 차이로 빈 결과가 나온 사례.

## 근거 자료

주장을 뒷받침하는 실측·과거 기록이다.

- [measurements/](measurements/) — 현장에서 재현한 개별 실측 기록 모음이다.
  - [measurements/2026-09-13-camera-fov.md](measurements/2026-09-13-camera-fov.md) — 카메라 화각.
  - [measurements/2026-09-13-stationary-ota.md](measurements/2026-09-13-stationary-ota.md) — 정지 상태 OTA.
  - [measurements/2026-09-14-camera-frame.md](measurements/2026-09-14-camera-frame.md) — 카메라 프레임(공유기 근접 · PC 유선).
  - [measurements/2026-09-14-camera-link-stability.md](measurements/2026-09-14-camera-link-stability.md) — 카메라 무선 링크 장시간 안정성.
  - [measurements/2026-09-15-camera-soak.md](measurements/2026-09-15-camera-soak.md) — 카메라 장시간 연속 수신.
  - [measurements/2026-09-15-e2e-chain.md](measurements/2026-09-15-e2e-chain.md) — NFR-1.1 E2E 단일 프레임 사슬.
  - [measurements/2026-09-18-turn-rate-curve.md](measurements/2026-09-18-turn-rate-curve.md) — 선회율 곡선과 그 재는 법.
  - [measurements/2026-09-22-inplace-rotation.md](measurements/2026-09-22-inplace-rotation.md) — 제자리 회전.
  - [measurements/2026-09-23-bridge-transparency.md](measurements/2026-09-23-bridge-transparency.md) — 0x34 브리지 투명성과 MP3 모듈 0x7B.
  - [measurements/2026-09-23-camera-tilt.md](measurements/2026-09-23-camera-tilt.md) — 카메라 틸트 각도 선정 근거.
  - [measurements/2026-09-23-xiao-mic.md](measurements/2026-09-23-xiao-mic.md) — XIAO 마이크 인식 품질과 영상 동시 전송.
  - [measurements/2026-09-25-xiao-core-and-stream.md](measurements/2026-09-25-xiao-core-and-stream.md) — XIAO 코어 버전과 스트림 재측정.
  - [measurements/2026-10-06-fallen-remeasure.md](measurements/2026-10-06-fallen-remeasure.md) — 쓰러진 사람 재실측(기존 YOLOX·VLM).
  - [measurements/2026-10-06-field-patrol.md](measurements/2026-10-06-field-patrol.md) — 2026-10-06 현장 순찰 실측 데이터.
- [lidar/](lidar/) — LiDAR 측위·항법 기록이다.
  - [lidar/STATUS_20261006.md](lidar/STATUS_20261006.md) — 2026-10-06 기준 LiDAR·항법 방향과 상태.
  - [lidar/LOCALIZATION_CALIBRATION_20261003.md](lidar/LOCALIZATION_CALIBRATION_20261003.md) — 구역 A 전역 정합 보정.
  - [lidar/NAV_GUARDS_20261003.md](lidar/NAV_GUARDS_20261003.md) — 주행 관문 A 후속.
  - [lidar/LIDAR_INPUT_HEALTH_20261004.md](lidar/LIDAR_INPUT_HEALTH_20261004.md) — LiDAR 입력 품질 진단.
  - [lidar/MAP_REFINEMENT_PREP.md](lidar/MAP_REFINEMENT_PREP.md) — 자동보정 관측 순찰의 PC 사전 준비.
- [archive/](archive/) — 더는 정본이 아닌 지난 문서를 보관한다.
- [PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md) — 배포 PPE 모델 ppe-v5 의 XIAO 검수 절차와 실측 기록(직립 4구간 합격)이다.
- [ppe-data-card.md](ppe-data-card.md) — 배포 PPE 모델 ppe-v5 의 데이터 출처·라이선스·분할·학습 설정·알려진 한계다.
- [../field_tests/README.md](../field_tests/README.md) — 실기 시험 원자료 폴더의 색인과 대표 기록이다.
- [../tools/README.md](../tools/README.md) — 운영·측정·개발 보조 스크립트의 분류표다.
- [../experiments/README.md](../experiments/README.md) — 런타임에 들어가지 않는 보관용 시제품(WonderEcho 음성)이다.

## 팀 운영 문서

작업 관리·요구사항 원본·하드웨어 절차·실측 절차 등 팀 내부에서 쓰는 문서는
[internal/](internal/) 에 모여 있다. 안내는 [internal/README.md](internal/README.md) 를 본다.
