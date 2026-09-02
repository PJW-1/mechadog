# <!-- feat(motion): 명령 타임아웃 감시기 추가 -->

## 대상 워크패키지

- **WBS ID**: <!-- 예: 3.2.1 -->
- **연계 요구사항**: <!-- 예: FR-1.3, NFR-2.1 -->

## 변경 내용

<!-- 무엇을 했는지는 diff에 있습니다. "왜" 이렇게 했는지를 적어주세요. -->

## 완료 기준 (DoD) 충족 근거

> WBS 사전의 해당 완료 기준을 그대로 인용하고, 충족했다는 근거를 적습니다.

**DoD**: <!-- 예: 송신 중단 후 300ms 이내 move(0,0) 진입을 타임스탬프 로그로 입증 -->

**근거**:
<!-- 로그 발췌 / 측정치 / 스크린샷 / 테스트 결과 -->

## 검증 방법

- [ ] `ruff check .` 통과
- [ ] `pytest -q` 통과
- [ ] 펌웨어 컴파일 확인 (해당 시)
- [ ] **실기 검증** — 개체: <!-- REF / dev1 / dev2 -->
- [ ] 실측값을 `docs/HARDWARE_VERIFICATION.md`에 기록 (해당 시)

## 체크리스트

- [ ] 하드코딩된 매직 넘버 없음 (전부 `config.yaml`)
- [ ] `print()` 대신 구조화 로거 사용
- [ ] 외부 I/O에 타임아웃 설정
- [ ] 판단 로직을 HAL 호출과 분리 (pytest 가능)
- [ ] **온보드 안전 로직(Tier 1)을 Host PC로 옮기지 않았음**

## ⚠️ 통신 규약 변경 여부

- [ ] **이 PR은 메시지 스키마를 변경하지 않습니다**

<!-- 스키마를 변경하는 경우 아래를 모두 체크하고 3인 전원 리뷰를 받으세요 -->
- [ ] 3인 전원 합의 완료
- [ ] `host/common/protocol.py` 수정
- [ ] `firmware_mechdog_motion/src/command_parser.*` 수정
- [ ] `host/behavior/commander.py` 수정
- [ ] `tools/mock_mechdog.py` 수정
- [ ] `tests/test_protocol.py` 갱신
