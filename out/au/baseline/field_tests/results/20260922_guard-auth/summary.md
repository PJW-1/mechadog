# 2026-09-22 경비 모드 휴대폰 ArUco 실기

## 조건

- 기체 `mechdog-01` (`192.168.1.101`), XIAO `192.168.1.102` (OV3660, VGA, `rot=180`)
- 호스트 `guard` 모드, 순찰 없이 기동한 뒤 안전 잠금 해제 및 순찰 시작
- 휴대폰에 `DICT_4X4_50` 승인 마커 ID 0 (`EMP-001`) 표시
- 재부팅 후 `STOP` ACK 정상, 텔레메트리 12초 120건·10.01Hz·누락 0건

## 관측

- `16:29:49` — 추적 ID 2에 `auth_granted(marker_id=0, holder=EMP-001, valid_s=60)`.
  같은 시각 `AUTH_WAIT → PATROL` (`AUTH_OK`), 단계 `L2 → L0`.
- 첫 승인 약 0.2초 뒤 `auth_session_dropped(track_id=2, reason=track_lost)`.
- `16:30:42` — 추적 ID 15에 같은 승인 마커로 다시 `auth_granted` 및 `AUTH_OK`.
- 두 번째 승인 약 0.3초 뒤에도 `auth_session_dropped(track_id=15, reason=track_lost)`.
- `16:30:52` — 다시 열린 `AUTH_WAIT`에서 `unauthenticated_left`로 `L2 → L3`.
  승인 후 추적 ID 상실이 재인증 요구의 직접 원인이지만, 사람 이동·휴대폰 가림·추적기 불안정 중
  무엇이 ID 상실을 일으켰는지는 미확정.
- 이후 순찰 중지, `ESTOP` 송신. 최종 로봇 텔레메트리는 `FAILSAFE`, `safety_latched=true`.
  호스트 런타임과 임시 마커 이미지 서버는 종료함.

## 판정 및 다음 단계

- **승인 마커 읽기와 `AUTH_OK` 복귀는 실기 통과.**
- **경비 인증 종단 동작 전체는 보류.** 승인 후 추적 ID 변경·재인증·`L3`를
  재현해 사람 이동/휴대폰 가림과 추적기 불안정을 구분해야 한다.
- 미등록 ID 7 거절과 30초 시간초과는 이번에 수행하지 않았다.
- 원본 런타임 로그: `logs/mechdog-01.jsonl` (로컬 로그, 저장소 추적 대상 아님).
