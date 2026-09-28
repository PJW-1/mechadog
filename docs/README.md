# 문서 색인

이 저장소의 문서는 독자에 따라 세 갈래로 나뉜다. 처음 본다면 **시스템 이해**부터 읽는다.

## 시스템 이해 (외부 독자용)

무엇을 만들었고 왜 이렇게 만들었는지 알고 싶다면 여기부터 본다.

- [ARCHITECTURE.md](ARCHITECTURE.md) — 시스템 구조·상태 머신·품질 기준과 용어를 정리한다.
- [design-decisions.md](design-decisions.md) — 시스템의 모양을 정한 대표 설계 결정을 골라 요약한다.
- [DECISIONS.md](DECISIONS.md) — 검토했으나 채택하지 않은 대안과 그 근거를 남긴다.
- [PROTOCOL.md](PROTOCOL.md) — Host와 로봇 펌웨어가 주고받는 통신 메시지의 정본이다.
- [PROTOCOL_LIDAR.md](PROTOCOL_LIDAR.md) — LiDAR 중계 노드 링크를 위한 PROTOCOL 의 확장 규약이다.
- [DASHBOARD.md](DASHBOARD.md) — 관제 서버와 FPV 화면이 텔레메트리·FSM 상태를 내보내는 방식을 설명한다.
- [ENGINEERING_GUIDE.md](ENGINEERING_GUIDE.md) — 로깅·테스트·CI 를 짤 때 따르는 구현 기준이다.

## 기능별 동작 흐름

준비 중 — 기능별 동작 흐름은 `docs/features/` 에 문서로 추가될 예정이다.

## 사례 연구

준비 중 — 사례 연구는 `docs/case-studies/` 에 문서로 추가될 예정이다.

## 근거 자료

주장을 뒷받침하는 실측·과거 기록이다.

- [measurements/](measurements/) — 현장에서 재현한 개별 실측 기록 모음이다.
- [archive/](archive/) — 더는 정본이 아닌 지난 문서를 보관한다.
- [PPE_ACCEPTANCE.md](PPE_ACCEPTANCE.md) — PPE 검수 절차다. (개발 중)

## 팀 운영 문서

작업 관리·요구사항 원본·하드웨어 절차·실측 절차 등 팀 내부에서 쓰는 문서는
[internal/](internal/) 에 모여 있다. 안내는 [internal/README.md](internal/README.md) 를 본다.
