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

실기에서 드러난 문제를 증상 → 가설과 배제 → 측정 → 원인 → 수정 → 검증 → 남은 한계 순서로 정리한다. 목록은 [case-studies/README.md](case-studies/README.md) 에 있다.

- [case-studies/01-command-timeout.md](case-studies/01-command-timeout.md) — 명령 타임아웃 300ms → 600ms.
- [case-studies/02-person-gate-time-window.md](case-studies/02-person-gate-time-window.md) — 10fps 추론이 진짜 검출의 절반 이상을 놓친 문제.
- [case-studies/03-echoed-state-self-lock.md](case-studies/03-echoed-state-self-lock.md) — 반향된 상태값 때문에 Host 가 스스로를 잠글 수 있었던 문제.
- [case-studies/04-i2c-voice-relay.md](case-studies/04-i2c-voice-relay.md) — 로봇 I²C 경유 음성 파형 중계가 불가능했던 사례.

## 근거 자료

주장을 뒷받침하는 실측·과거 기록이다.

- [measurements/](measurements/) — 현장에서 재현한 개별 실측 기록 모음이다.
- [archive/](archive/) — 더는 정본이 아닌 지난 문서를 보관한다.
- [PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md) — PPE 검수 절차다. (개발 중)
- [../field_tests/README.md](../field_tests/README.md) — 실기 시험 원자료 폴더의 색인과 대표 기록이다.
- [../tools/README.md](../tools/README.md) — 운영·측정·개발 보조 스크립트의 분류표다.

## 팀 운영 문서

작업 관리·요구사항 원본·하드웨어 절차·실측 절차 등 팀 내부에서 쓰는 문서는
[internal/](internal/) 에 모여 있다. 안내는 [internal/README.md](internal/README.md) 를 본다.
