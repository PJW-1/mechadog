# 협업 규칙 (Contributing)

> 팀 안의 작업 관리 절차(WBS·완료 판정·주간 리뷰)는 [TEAM_PROCESS](docs/internal/TEAM_PROCESS.md)
>
> **처음이라면 [README](README.md)의 문서 지도를 먼저 보세요.**
> **절차는 이 문서, 구현 기준은 [엔지니어링 가이드](docs/ENGINEERING_GUIDE.md)** (로깅·테스트·CI)
> **메시지 스키마 정본은 [통신 프로토콜](docs/PROTOCOL.md)** — 팀장이 결정하여 전파한다
>
> **환경 세팅**: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` — 기준값 약 40초.
> 크게 초과하면 원인을 이슈로 알려 주세요.

---

## 1. 기준기(Reference Unit)

게이트 검수를 수행하는 지정 개체다. **LiDAR 가 1대뿐이라 Phase 별로 나눈다.**

| 구분 | 정의 | 검수 범위 |
| :--- | :--- | :--- |
| **`phase1_reference`** | **Phase 1 표준 구성**(LiDAR 미장착)의 지정 개체 | G1·G2·G3 검수, **Phase 1 NFR 성능 수치의 출처** |
| **`phase2_reference`** | 측위 센서(LiDAR)를 장착한 지정 개체 | Phase 2 측위·매핑 검수 |

- 두 기준기는 지정해 물리적으로 표시한다.
- LiDAR 는 1대뿐이며 **`mechdog-02` 에 장착해 `phase2_reference`** 로 지정한다. `mechdog-02` 는 시연 기체이기도 하다.
- **게이트 검수는 반드시 해당 Phase 의 기준기에서** 수행한다. 개발기 통과는 검수로 인정하지 않는다.
- 권장 배치: **LiDAR 미장착 개체를 `phase1_reference`** 로 둔다. Phase 1 표준 물리 구성이 유지되어 NFR 수치의 기준이 흔들리지 않는다.

---

## 2. 브랜치 전략

**2단 구조**다. `main`은 릴리스 브랜치이고, 일상 작업은 `dev`에 모인다.

```
main ──────────────●───────────────────────●──────▶  릴리스. 소유자만 머지
                  ╱                       ╱
dev  ───●────●───●─────●────●────●───────●────────▶  통합. PR로만 병합
        ╱    ╱         ╱    ╱    ╱
feature ●    ●         ●    ●    ●                   작업 브랜치
```

| 브랜치 | 용도 | 저장소 운영 정책(의도) |
| :--- | :--- | :--- |
| **`main`** | 릴리스·시연에 쓰는 안정 버전 | PR 필수 · CI 통과 필수 · **Code Owner(@PJW-1) 승인 필수** · 강제푸시·삭제 금지 |
| **`dev`** | 통합 브랜치. 모든 기능이 여기 모인다 | PR 필수 · CI 통과 필수 · 리뷰 권장(필수 아님) · 강제푸시·삭제 금지 |
| `feature/<설명>` | 작업 브랜치 | 자유. `dev`로 PR |
| `fix/<설명>` | 버그 수정 | `dev`로 PR |
| `docs/<설명>` | 문서만 수정 | `dev`로 PR |

> **GitHub 설정 확인 주의** — [CODEOWNERS](CODEOWNERS)는 리뷰어를 지정할 뿐, 파일 하나만으로 승인을 강제하지 않는다.
> 실제 강제에는 `main`의 branch protection/ruleset에서 **PR·필수 CI·Code Owner 승인·강제 푸시 금지**를 켜야 한다.
> 2026-09-08 `main`과 `dev` 모두 보호 상태이며 **Python Quality, Firmware Quality,
> MechDog-Motion 빌드, XIAO-Vision 빌드** 네 검사가 머지 필수다. `main`은 Code Owner 승인 1건도
> 요구하며, 두 브랜치 모두 강제 푸시·삭제를 막는다.

### 작업 흐름

```bash
git switch dev && git pull
```

```bash
git switch -c feature/command-timeout
```

작업 후 `dev`로 PR을 올린다. **`main`으로 직접 PR을 올리지 않는다.**

### `dev` → `main` 승격

게이트 검수(G1~G3)를 통과한 시점, 또는 소유자가 기준선을 따로 정한 시점에만 소유자가 `dev` → `main` PR을 만들어 머지한다.

| 시점 | 태그 |
| :--- | :--- |
| G1 통과 (M1 완료) | `v0.1.0` |
| G2 통과 (M2 완료) | `v0.2.0` |
| 문서 포트폴리오 정리 기준선 (게이트 아님) | `v0.3.0` |
| G3 통과 (M3 완료) | `v1.0.0` |

승격 PR 을 올릴 때 지키는 것:

- `main` 은 «최신 상태 필수» 로 보호된다. 직전 승격의 병합 커밋이 `dev` 에 없으면 승격 PR 이 `BEHIND` 로 막히므로, 먼저 `main` → `dev` PR 로 그 커밋을 들인다. 파일 변화는 없다.
- head 가 `main` 인 PR 에는 GitHub 의 «브랜치 최신화»(update-branch)를 부르지 않는다. `dev` 가 `main` 에 곧바로 합쳐져 승격 PR 과 리뷰를 건너뛴다.
- 펌웨어 범위 검사는 `dev` → `main` PR 에서 건너뛴다. 각 PR 이 `dev` 에 들어올 때 이미 통과했기 때문이다.
- 병합 뒤 `main` 머리에 SemVer 태그를 올리면 CI 의 릴리스 잡이 태그가 `main` 에 있는지 확인하고 펌웨어를 첨부한 릴리스를 만든다.

## 3. 커밋 메시지

```
<type>(<scope>): <요약>

<본문 — 왜 이렇게 했는지. 무엇을 했는지는 diff에 있음>

Refs: #12
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

Refs: #12
```

---

## 4. PR 규칙

| 항목 | 규칙 |
| :--- | :--- |
| **대상 브랜치** | **`dev`.** `main` 반영은 소유자가 릴리스 시점에만 수행한다 |
| 크기 | 하나의 응집된 변경으로 묶는다. 관련 이슈는 PR 본문에 모두 적는다 |
| 리뷰어 | **`dev` 는 승인 없이 머지 가능** (CI 통과가 게이트). 리뷰는 권장이며 강제하지 않는다. **`main` 은 소유자 승인 필수** |
| CI | 전 잡 통과 필수 |
| 검증 | 변경을 어떻게 확인했는지(테스트 결과·실기 확인)를 PR 본문에 적는다 |
| 비밀 | 비밀번호·토큰·Wi-Fi 정보(`wifi_secrets.h`, `config/secrets.yaml`)는 커밋하지 않는다 |

---

Issue 를 닫으려면 PR 본문에 `Closes #이슈번호`를, 일부만 다루면 `Refs #이슈번호`를 적는다.

## 5. 통신 규약 변경 규칙

메시지 스키마(`host/common/protocol.py`)는 **보내는 쪽(C)과 받는 쪽(A)을 다른 사람이 만드는 유일한 지점**이다. 불일치하면 로봇이 조용히 반응하지 않고, 서로 상대방 코드를 의심하게 된다.

**그래서 규칙보다 자동 검증을 우선한다.** 사람의 규칙 준수에 의존하지 않는다.

### 5.1 변경 유형에 따라 절차가 다르다

| 유형 | 예 | 절차 |
| :--- | :--- | :--- |
| **하위 호환 (additive)** | 새 `type` 추가 (`LED`, `SOUND` 등), 옵션 필드 추가 | **자유롭게 진행.** PR 본문에 변경 사실만 기재 |
| **파괴적 (breaking)** | 필드명·단위·의미·범위 변경, 타입 삭제 | 이슈로 알리고 **아래 5.3의 4곳을 함께 수정** |

**왜 추가는 자유로운가** — 수신측이 **미지의 `type` 을 폐기하고 WARN 로그를 남기도록** 명세되어 있다(PRD FR-5.1 규칙 ④). 따라서 한쪽이 먼저 새 타입을 보내기 시작해도 기존 동작에 영향이 없고, 다른 쪽이 나중에 핸들러를 추가하면 그 시점부터 동작한다.

```
2주차: C 가 LED 명령 송신 시작  →  A 는 아직 핸들러 없음
        → ESP32 가 미지 타입으로 폐기 (기존 동작 영향 0)
3주차: A 가 핸들러 추가          →  그 시점부터 동작
```

### 5.2 자동 검증 (골든 픽스처)

`tests/fixtures/protocol_samples.jsonl` 에 **정본 메시지 예시**를 둔다.

| 검증 대상 | 방법 |
| :--- | :--- |
| Python 직렬화 | `test_protocol.py` 가 픽스처와 대조 |
| C++ 파서 | 같은 픽스처를 호스트 컴파일로 파싱하여 대조 |
| 필드 범위·클램핑 | 경계값 케이스를 픽스처에 포함 |

**스키마를 바꾸면 픽스처도 바꿔야 하고, 한쪽만 바꾸면 CI 가 실패한다.** 이것이 불일치를 막는 실질적 장치다.

### 5.3 파괴적 변경 시 함께 수정할 곳

- `host/common/protocol.py` — Python 직렬화
- `firmware_mechdog_motion/src/command_parser.*` — C++ 파서
- `tools/mock_mechdog.py` — 목업
- `tests/fixtures/protocol_samples.jsonl` — 제어 명령 정본 픽스처
- `tests/fixtures/telemetry_samples.jsonl` — **텔레메트리 정본 픽스처** (안전 임계 경계값 포함)

### 5.4 규약은 팀장이 정해 전파한다

스키마는 합의 절차를 두지 않는다. 정본은 **[docs/PROTOCOL.md](docs/PROTOCOL.md)** 이며,
파서를 구현할 때는 PRD 가 아니라 그 문서를 본다.

기억할 것은 한 줄이다.

> **필드를 추가하는 건 언제든 해도 된다. 바꾸거나 지우는 것만 팀장에게 말한다.**

나머지는 CI 가 잡는다.

---

## 6. 하드웨어 관련 규칙

| 규칙 | 내용 |
| :--- | :--- |
| **개체 프로파일 필수** | 모든 실기 실행은 `--device <unit-id>` 로 자신의 프로파일을 지정한다. 기본값 사용 금지 |
| **보정값은 기체별로 잰다 — 복사 금지** | `servo_offset` 과 `gait_calibration` 은 **그 기체에서 직접 재서** 그 기체의 프로파일에만 적는다. 다른 기체의 값을 복사하거나 2대 공용으로 쓰는 PR 은 반려한다. 재는 절차는 [docs/measurements/2026-09-18-turn-rate-curve.md](docs/measurements/2026-09-18-turn-rate-curve.md) 1절에 있고, 실측 사례는 [TEAM_PROCESS 4절](docs/internal/TEAM_PROCESS.md)에 있다 |
| **확인 결과는 PR 본문에** | 전압·fps·지연 확인 결과는 PR 본문에 남긴다. 채팅이나 메모에만 남기지 않는다. **`config` 로 들어갈 값은 `config/` 가 정본**이고 별도 리포트 문서는 두지 않는다. 원자료(csv·json·콘솔 로그)까지 남길 때만 `TEST_MECHDOG/results/<날짜-시각>/` 에 둔다 |
| **성능 수치의 출처 명시** | Phase 1 NFR 측정치는 **`phase1_reference` 실측값**으로 문서화하고, 개체별 편차는 참고치로 병기한다 |
| **안전 로직은 온보드에서 이동 금지** | 초음파 반사 정지·명령 타임아웃·저전압·전도 감지는 Tier 1이다. Host PC로 올리는 PR은 반려한다 ([아키텍처 1.2 불변 규칙](docs/ARCHITECTURE.md)) |
| **카메라·로봇 펌웨어 코드는 한 PR에 섞지 않는다** | `firmware_xiao_vision`은 DR-3 상 촬영·송출 전용이다. 한 PR이 카메라와 `firmware_mechdog_motion`/`firmware_lidar_relay` 의 **코드 파일**(`*.ino`·`*.cpp`·`*.h` 등)을 같이 바꾸면 반려한다 — 로봇 기능이 카메라에 딸려 플래시되는 사고가 있었다 (2026-09-23). 문서 동반 변경은 허용. CI 의 `Firmware scope guard` 가 자동으로 검사한다 (`tools/check_firmware_scope.py`). 카메라 펌웨어 PR 은 실기 플래시 후 부팅 로그와 스트림 1프레임을 본문에 남긴다 |

---

## 7. 코드 규약 — 4대 엔지니어링 축

| 축 | 규칙 |
| :--- | :--- |
| **① 파라미터화** | **매직 넘버 금지.** 모든 상수는 `config/config.yaml`에서 로드. 하드코딩된 숫자가 있는 PR은 반려 |
| **② 예외 처리** | 모든 외부 I/O(소켓·HTTP·시리얼)에 타임아웃. 실패 시 안전측(정지) 판단 |
| **③ 성능** | 추론은 워커 스레드로 분리. 프레임 큐는 최신 우선 드롭 |
| **④ 로깅** | `print()` 금지. `common/logging_setup.py` 사용. 모든 로그에 `seq`·`ts`·`state`·`device_id` 포함 |

**HAL 분리 원칙** — 판단 로직(FSM·안전 판정·패킷 파싱)은 하드웨어 호출과 분리하여 작성한다. **하드웨어 없이 pytest로 검증 가능해야 한다.**

### 넘어서는 안 되는 선

| 규칙 | 근거 |
| :--- | :--- |
| **Tier 1 안전 로직을 Host PC 로 옮기지 않는다** | [아키텍처 1.2](docs/ARCHITECTURE.md) 불변 규칙. PC 가 꺼져도 로봇은 스스로 멈춘다 |
| **안전 로직에 상태 억제 플래그를 쓰지 않는다** | DR-16. 임계값 조정으로 해결한다 |
| **온보드 코드에 블로킹 지연을 두지 않는다** | 보행 제어 루프 방해 금지 (NFR-1 비목표) |
| **거리(미터)를 구하려 하지 않는다** | DR-15. 측정 수단이 없다. bbox 로 직접 판정한다 |
| **제자리 회전을 전제하지 않는다** | DR-11. 기구 구조상 불가 — 라이브러리 한계가 아니다 |

---

## 8. 로컬 검증 (PR 전)

```bash
ruff check . && ruff format --check .
python -m pytest -q --cov=host --cov=tools --cov-fail-under=80
```

`requirements.txt`·`requirements-dev.txt` 를 고쳤으면 CI 용 잠금 파일을 다시 만든다 (Linux·CPython 3.12 기준, 해시 포함). Windows 개발 PC 는 예전처럼 `requirements*.txt` 로 설치한다.

```bash
pip install uv
for f in requirements requirements-dev; do
  uv pip compile $f.txt --python-platform x86_64-manylinux_2_28 --python-version 3.12 --generate-hashes -o $f.lock
done
```

CI 의 커버리지 게이트는 80% 다. 대시보드 웹 시험은 `host/dashboard` 에서 `npm test`(`node --test web-tests/*.test.mjs`)로 돈다.

**검출을 실제로 돌려 볼 때만** 가중치가 추가로 필요하다 (시험은 없이도 전부 돈다).

```bash
python tools/fetch_models.py
```

> ⚠️ **`onnxruntime` 과 `onnxruntime-directml` 을 같이 깔면 안 된다.** 둘은 같은
> `onnxruntime` 이름을 점유하는데, **에러 없이 한쪽이 다른 쪽을 가린다.** 실제로
> 이 PC 에 둘 다 깔려 있었고 DirectML 이 목록에서 사라져 **CPU 로 도는 것을 몰랐다**
> (61.9ms → 교체 후 8.2ms). Windows 는 `onnxruntime-directml` **하나만** 남긴다.
>
> ```bash
> pip uninstall -y onnxruntime onnxruntime-directml
> pip install onnxruntime-directml     # Windows
> python -c "import onnxruntime as o; print(o.get_available_providers())"
> ```
>
> `DmlExecutionProvider` 가 보여야 한다. 기동 로그의 `execution_provider` 로도
> 확인된다 — **CPU 로 떨어지면 `WARNING`** 이 남는다.

> ⚠️ **개발 콘솔은 cp949 다 — `print()` 에 ASCII·한글 밖의 문자를 넣으면 죽는다.**
> `—`(U+2014)·`·`(U+00B7) 같은 문자를 만나면 `UnicodeEncodeError` 가 나고, 그 줄이
> 기동 배너면 **프로그램 자체가 시작되지 않는다** (실제로 `runtime` 의 콘솔 안내와
> `fetch_models.py` 에서 각각 한 번 겪었다). 로거는 살아남는다 — 콘솔 핸들러가
> 치환하기 때문이며 `print()` 만의 문제다.
>
> - 파일로 쓸 때는 `encoding="utf-8"` 을 명시한다.
> - 콘솔로 낼 때는 **ASCII 와 한글만** 쓰거나, `fetch_models.py` 처럼
>   `io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")` 로 감싼다.
> - **`ruff` 와 `pytest` 는 이것을 잡지 못한다** — 시험은 `print` 를 지나가지 않는다.

MechDog 펌웨어는 기본적으로 서보를 초기화하지 않는 dry-run 구성으로 컴파일한다.
실제 구동용 외부 라이브러리 결합과 안전 시험 절차는
`firmware_mechdog_motion/README.md`의 *"구동·센서 통합 빌드 준비"* 절을 따르고,
준비 상태는 `python tools/firmware_env.py`로 점검한다. **벤더 파일은 절대 커밋하지 않는다**(ADR-20).

펌웨어 포맷과 컴파일 확인:

```bash
clang-format --dry-run --Werror firmware_mechdog_motion/src/*.cpp firmware_mechdog_motion/src/*.h
arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion
```

> `clang-format` 은 `requirements-dev.txt` 에 들어 있다. **없으면 포맷 위반을 CI 에서만 알게 되고
> 그때는 이미 PR 이 빨간불이다** — 실제로 그렇게 한 번 겪었다.
