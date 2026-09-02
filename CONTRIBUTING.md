# 협업 규칙 (Contributing)

> 3인 팀 · 4주 · [WBS](docs/WBS.md) 기준

---

## 1. 역할 및 담당 영역

| 역할 | 담당 WBS | 주 디렉터리 |
| :--- | :--- | :--- |
| **A · 임베디드** | 2.0 하드웨어, 3.2 안전로직, 4.1 모션펌웨어, 5.1·5.3.2~3 | `firmware_mechdog_motion/`, `third_party/` |
| **B · 인지·AI** | 3.3 인지로직, 4.2 비전펌웨어, 4.3 스트림, 4.6 화면, 6.1.2·6.3 | `firmware_xiao_vision/`, `host/vision/`, `host/dashboard/static/` |
| **C · 시스템·통합** | 1.0 문서, 3.1 통신규약, 3.4 FSM, 4.5 서버, 5.2·5.3.1, 6.2·6.4 | `host/behavior/`, `host/dashboard/`, `host/common/`, `tests/`, `docs/` |

**기준기(REF)** = B의 로봇. 측위 센서는 여기에만 장착하며, **게이트 검수는 반드시 기준기에서** 수행한다.

---

## 2. 브랜치 전략

```
main ────────●────────────●────────────●──────▶   보호됨. PR로만 병합
              ╲          ╱ ╲          ╱
   feature/*   ●────────●   ●────────●
```

| 브랜치 | 규칙 |
| :--- | :--- |
| `main` | **직접 푸시 금지.** PR + CI 통과 필수 |
| `feature/<wbs-id>-<설명>` | 작업 브랜치. 예: `feature/3.2.1-command-timeout` |
| `fix/<설명>` | 버그 수정 |
| `docs/<설명>` | 문서만 수정 |

**브랜치명에 WBS ID를 넣는다.** 어떤 워크패키지의 작업인지 추적되어야 한다.

```bash
git switch -c feature/3.2.1-command-timeout
```

---

## 3. 커밋 메시지

```
<type>(<scope>): <요약>

<본문 — 왜 이렇게 했는지. 무엇을 했는지는 diff에 있음>

Refs: WBS 3.2.1, FR-1.3
```

| type | 용도 |
| :--- | :--- |
| `feat` | 기능 추가 |
| `fix` | 버그 수정 |
| `docs` | 문서 |
| `test` | 테스트 추가·수정 |
| `refactor` | 동작 변경 없는 구조 개선 |
| `chore` | 빌드·설정·의존성 |

| scope | 대상 |
| :--- | :--- |
| `motion` | MechDog 펌웨어 |
| `vision` | XIAO 펌웨어 / 비전 파이프라인 |
| `fsm` | 행동 상태 머신 |
| `dash` | 대시보드 |
| `proto` | 통신 규약 |
| `ci` | 파이프라인 |

**예시**

```
feat(motion): 명령 타임아웃 감시기 추가

300ms 무명령 시 move(0,0)으로 정지한다. 마지막 명령을 계속
실행하면 Wi-Fi 단절 시 로봇이 벽에 충돌하므로 온보드에 둔다.

Refs: WBS 3.2.1, FR-1.3, NFR-2.1
```

---

## 4. PR 규칙

| 항목 | 규칙 |
| :--- | :--- |
| 크기 | **워크패키지 1개 = PR 1개.** 여러 WBS를 한 PR에 섞지 않는다 |
| 리뷰어 | 최소 1명. **통신 규약(`host/common/protocol.py`) 변경 시 3인 전원** |
| CI | 전 잡 통과 필수 |
| DoD | [WBS 사전](docs/WBS.md)의 해당 워크패키지 완료 기준을 PR 본문에 인용하고 충족 근거를 적는다 |

---

## 5. ⚠️ 통신 규약 동결 (가장 중요한 규칙)

**WBS 3.1.1 메시지 스키마는 1주차에 3인 합의로 동결한다.**

동결 이후 스키마를 변경하려면:

1. 이슈를 열어 변경 사유를 기술
2. **3인 전원 합의**
3. 아래 4곳을 **동시에** 수정
   - `host/common/protocol.py` (Python 직렬화)
   - `firmware_mechdog_motion/src/command_parser.*` (C++ 파서)
   - `host/behavior/commander.py` (송신)
   - `tools/mock_mechdog.py` (목업)
4. `tests/test_protocol.py` 갱신

> 한쪽만 바꾸면 **원인을 찾기 매우 어려운 통신 버그**가 됩니다. 스키마는 3인의 작업이 만나는 유일한 접점입니다.

---

## 6. 하드웨어 관련 규칙

| 규칙 | 내용 |
| :--- | :--- |
| **개체 프로파일 필수** | 모든 실기 실행은 `--device <unit-id>` 로 자신의 프로파일을 지정한다. 기본값 사용 금지 |
| **실측값은 문서에 기록** | 무게·전압·fps·지연 측정치는 반드시 [HARDWARE_VERIFICATION.md](docs/HARDWARE_VERIFICATION.md)에 기입한다. 채팅이나 메모에만 남기지 않는다 |
| **성능 수치의 출처 명시** | NFR 측정치는 **기준기 실측값**으로 문서화하고, 개체별 편차는 참고치로 병기한다 |
| **안전 로직은 온보드에서 이동 금지** | 초음파 반사 정지·명령 타임아웃·저전압·전도 감지는 Tier 1이다. Host PC로 올리는 PR은 반려한다 ([PRD 2.2 불변 규칙](docs/PRD_Physical_AI_Guard_Robot.md)) |

---

## 7. 코드 규약 — 4대 엔지니어링 축

| 축 | 규칙 |
| :--- | :--- |
| **① 파라미터화** | **매직 넘버 금지.** 모든 상수는 `config/config.yaml`에서 로드. 하드코딩된 숫자가 있는 PR은 반려 |
| **② 예외 처리** | 모든 외부 I/O(소켓·HTTP·시리얼)에 타임아웃. 실패 시 안전측(정지) 판단 |
| **③ 성능** | 추론은 워커 스레드로 분리. 프레임 큐는 최신 우선 드롭 |
| **④ 로깅** | `print()` 금지. `common/logging_setup.py` 사용. 모든 로그에 `seq`·`ts`·`state`·`device_id` 포함 |

**HAL 분리 원칙** — 판단 로직(FSM·안전 판정·패킷 파싱)은 하드웨어 호출과 분리하여 작성한다. **하드웨어 없이 pytest로 검증 가능해야 한다.**

---

## 8. 로컬 검증 (PR 전)

```bash
ruff check . && ruff format --check .
pytest -q
```

펌웨어는 컴파일만 확인:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion
```

---

## 9. 주간 리뷰

주 1회(WBS 1.4):

- 워크패키지 진척 갱신 (완료 / 진행 / 미착수)
- 리스크 레지스터 RISK-01~09 상태 갱신
- 게이트 판정 (G0~G3)
- 담당자별 부하 편차 확인 → 필요 시 재배분
