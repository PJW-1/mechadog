# 음성 설정 데이터 관리

> ⚠️ **개정 (2026-09-23)**: 가상 MES(`mes_demo.db`·`factory_mes.py`·`factorylink.py`)와 현장지원 모드,
> 규칙 섹션 `factory_rules`, 호출어 종류 `status`·`machine`, 시나리오 `robot_briefing`을 폐기했다
> ([ADR-38](../../docs/DECISIONS.md#adr-38)). 공유 데이터는 음성3테이블과 규칙 JSON 4섹션뿐이다.
> WBS 4.7.15~18(`factory_ops/` 설계)도 함께 폐기됐다.

음성 DB 정본은 **3테이블(`settings`, `roster`, `phrases`)로 확정**했다.
8테이블 음성 DB는 이전 입력으로만 지원하며, 두 구조를 선택해서 운용하지 않는다.
운영자가 바꾸는 설정·명단·추가 응답은 DB,
고정 발화 규칙은 `voice_data.rules.json`으로 관리한다. PC에서 읽는 데이터이며
WonderEcho 펌웨어에 DB나 AI 모델을 넣지 않는다. 기능이나 명령 종류를 줄인 것이 아니다.

SQLite의 단일 정의는 `voice_schema.py`이며 생성·관리·가져오기·이전 도구가 함께 쓴다.
정본 `PRAGMA user_version`은 **2**다. 음성 실행 시작과 CLI 관리는 구형 규칙 테이블,
다른 영역의 테이블, 불완전한 컬럼/기본키, 알 수 없는 버전을 발견하면 이전 안내와 함께
중단한다. DB를 무시하고 기본 규칙으로 계속 실행하거나 자동 삭제하지 않는다.
다음 읽기 전용 검사로 설치 상태를 확인한다.

```powershell
python voice_store.py --check-schema
```

이전 최적화판의 정확한 3테이블/버전0 DB도 읽을 수 있다. 아래 이전 도구를 실행하면
전체 백업 후 데이터를 재시드하지 않고 버전2로 등록한다. 버전2를 다시 이전하면 변경하지 않는다.
공유 묶음도 음성3테이블만 담는다(이전의 “음성3 + MES5 = 8”은 폐기).
다른 영역의 테이블이 섞인 DB를 음성 실행 DB로 지정하면 거부한다.

**팀원이 같은 데이터를 설치하려면:** [공유 데이터 안내](demo/README.md)를 따라
`python prepare_demo.py --output-dir .`를 실행한다. 저장소의 합성 JSON에서
음성 DB와 규칙 JSON을 만들며, 기존 파일과 삭제한 직원 명단을 그대로 보존한다.

## 메인 안전 이력 DB와의 관계

사용자가 제공한 메인 ERD는 `robots → mission_runs → incidents`, `zones → incidents`로
누가·언제·어디서 순찰했고 어떤 안전 사건이 발생했는지 기록한다.
이번 도구는 이 네 테이블을 생성·수정하거나 가상 사건을 넣지 않는다.
`roster`의 이름을 `robot_id`에 임의로 연결하지도 않는다.
실제 연동은 운영상의 ID 대응과 이벤트 기록 계약이 정해진 뒤 별도로 구현한다.

| 영역 | 테이블/저장소 | 역할 |
| --- | --- | --- |
| 메인 안전 이력 | robots, mission_runs, incidents, zones | 로봇·순찰·안전 사건·구역 |
| 음성 운영 DB | settings, roster, phrases | 운영 설정·신원 명단·추가 응답 |
| 로컬 규칙 JSON | keywords, command_endings, action_commands, scenario_triggers | 호출어·명령·시나리오 분기 |
| 합성 문서 | knowledge/*.txt | 규정·매뉴얼·합성 시나리오 원문 (시나리오 정적 안내문) |

가상 MES 5테이블(`production_status` 등)과 `factory_ops/` 설계(WBS 4.7.15~18)는 2026-09-23
폐기했다. 실기 검증 없이 폐기됐으며 사건 JSONL은 기존 경로를 유지한다.

### 음성 DB의 실제 컬럼

| 테이블 | 컬럼 | 기본키 | 유지 이유 |
| --- | --- | --- | --- |
| settings | key TEXT, value TEXT, updated_at TEXT | key | 후속 발화 시간, STT 힌트, API 주소를 운영 중 변경 |
| roster | name TEXT, note TEXT | name | 신원 확인 명단을 추가·삭제 |
| phrases | category TEXT, phrase TEXT | category + phrase | 관제 화면에서 추가한 응답을 한 곳에서 관리 |

현재 명단은 이름을 키로 쓰는 **데모 명단**이다. 실제 동명이인·사원번호 기반 인증까지
지원한다는 뜻은 아니다. 메인 ERD와의 직원 ID 계약 없이 임의로 새 식별자를 만들지 않았다.
SQLite 시각은 시간대가 있는 ISO 문자열, Supabase settings.updated_at은 timestamptz다.
기존 PK가 조회에 쓰이므로 이 세 테이블에 중복 인덱스·별도 숫자 ID를 추가하지 않았다.

`phrases`는 추가 응답만 저장한다. 기본 응답은 `phrases.py`의 검증된 상수를 유지한다.
관리 화면의 조회·추가·삭제·음성 캐시 생성도 같은 테이블을 사용한다.
`phrases_custom.json`은 더 이상 쓰지 않고 아래 이전 도구로 가져온다.

### 고정 규칙은 JSON 한 파일

DB 이름이 `voice_data.db`면 규칙은 같은 폴더의 `voice_data.rules.json`이다.
파일이 없으면 각 소비자 모듈의 기존 기본값을 사용한다. 따라서 새 설치에 규칙 사본이나
DB가 필수는 아니다. 기존 사용자 설정이 있는 경우에는 아래 이전을 먼저 해야 한다.
파일은 `format_version: 1` 및 아래 4개 섹션 전체를 갖는다.

| JSON 섹션 | 형식·예시 | 의미 |
| --- | --- | --- |
| keywords | `{"wake": ["메카독"], ...}` | wake/sleep/resume/emergency 단어 |
| command_endings | `["해주세요", "해줘", ...]` | 명령에서 벗겨낼 어미, 긴 것부터 적용 |
| action_commands | `{"순찰시작": ["patrol_start", "순찰을 시작합니다."]}` | 즉시 실행할 허용 명령과 확인 멘트 |
| scenario_triggers | `{"신원확인": "guard", ...}` | 기존 시나리오 시작 구문 |

규칙 자체는 필요하지만 이 목록마다 DB 테이블을 둘 필요는 없다. action과 scenario는
실행 방식이 달라 섹션을 구분하고 동일 구문 충돌을 검증한다. 위 예시는 부분 설명이며,
전체 파일은 `python voice_store.py --rules`로 확인한다.

2026-09-23 이전에 만든 규칙 파일에는 폐기 항목이 남아 있을 수 있다. 읽을 때
`voice_rules.drop_retired`가 섹션 `factory_rules`, 호출어 종류 `status`·`machine`,
시나리오 `robot_briefing`을 가리키는 트리거를 걸러 내므로 파일을 손으로 고치지 않아도 된다.
구형 DB 이전(`from_legacy`)과 v1·v2 가져오기 묶음에도 같은 거르기가 적용된다.

`voice_rules.py`는 파일 변경 시에만 파싱/검증한다. 고정 규칙 조회에서는 SQLite·Supabase를
호출하지 않는다. 잘못된 JSON/미등록 action·scenario는 경고 후 기본 규칙을 사용한다.
CLI 저장은 검증 후 원자적으로 교체한다. 비상정지 3개 구문은 삭제해도 코드가 보완하며,
다른 action으로 재매핑하는 저장은 거부한다. 로봇 실행 화이트리스트와 런타임 게이트는 유지한다.
JSON 안의 빈 목록은 해당 규칙을 끄는 명시적 설정이다(비상정지 보호 구문 제외).

### 기존 8테이블 DB 이전

DB/설정 편집 프로세스를 종료한 뒤 실행한다. 서비스 중 자동 이전하지 않는다.
`phrases_custom.json`만 있고 DB가 없다면 먼저 `python prepare_demo.py --output-dir .`로
실행 DB를 만든 뒤 아래 명령을 실행한다. 기존 JSON의 문구는 이전 시 DB로 가져온다.

```powershell
python migrate_voice_db.py --db voice_data.db --check
python migrate_voice_db.py --db voice_data.db
python voice_store.py --db voice_data.db --check-schema
```

검증 → SQLite 전체 백업 → 유효한 규칙 JSON 저장 → 5개 규칙 테이블 제거 순서다.
구형 `factory_rules` 테이블은 제거만 하고 JSON으로 옮기지 않는다(폐기, ADR-38).
3개 운영 테이블의 행·시각은 유지하고, 규칙 순서와 기존의 빈 테이블 기본값 적용도 보존한다.
기존 JSON과 DB 규칙이 다르면 덮어쓰지 않고 중단한다. `phrases_custom.json`이 있으면
중복 없이 phrases에 넣고 커밋 후 `.migrated.bak` 사본으로 보관한다. 재실행은 중복을 만들지 않는다.
실패하면 DB 트랜잭션이 취소된다. JSON 저장 뒤 DB 커밋 전 프로세스가 끊기면 JSON이 남을 수
있지만 이전 DB와 같은 규칙이다. 원본 백업을 보존하고 동일 명령으로 재개한다.
되돌릴 때는 프로세스를 종료하고 백업 DB와 이전 코드를 함께 복구한다.

기존 **원격** 규칙 테이블은 자동 삭제하지 않는다. 원격에서 수정한 규칙이 있다면 먼저
행을 내보내서 v1 가져오기 묶음으로 검증하고 JSON으로 추출해야 한다. 로컬 DB 이전만으로
원격 맞춤 규칙까지 옮겨졌다고 간주하지 않는다. 새 런타임은 원격 고정 규칙을 조회하지 않는다.

## 보완한 동작

- `--seed`는 비어 있는 DB만 초기화한다. 기존 행이 하나라도 있으면 그대로 둔다.
- 의도적인 로컬 초기화는 `--seed --reset`으로 한다. 먼저 SQLite backup API로
  같은 폴더에 `DB파일명.UTC시각.bak`를 만든다. WAL에 있는 변경도 포함한다.
- 원격 `--seed --remote`는 기존 기본키 값을 덮어쓰지 않는다. 누락된 기본 행은 추가하므로
  삭제한 기본 명단을 유지해야 하는 운영 DB에는 다시 시드하지 않는다.
- `roster` DB를 사용 중이면 빈 명단·조회 오류를 **승인 대상 0명**으로 처리한다.
  원격 캐시가 만료된 후 오류가 나도 과거 명단이나 로컬 기본 명단으로 승인하지 않는다.
  캐시 유효기간은 기본 60초이므로 삭제 반영도 최대 그만큼 지연될 수 있다.
  DB 자체를 사용하지 않는 기존 데모 경로만 `직원명단.txt`를 읽는다.
- `supabase_setup.sql`은 음성3테이블의 스키마·인덱스·RLS만 적용한다. 자동 재시드는 없고,
  이전 SQL로 만든 가상 MES 테이블(폐기)은 자동 삭제하지 않으므로 필요하면 직접 정리한다.
  (이전의 `NOT VALID` 입력 제약은 MES 테이블 전용이라 함께 빠졌다.)

## 로컬 데이터를 가져오기

음성 PC의 기존 Python 환경에서 이 폴더로 이동해 실행한다. GPU·로봇·모듈 연결은 필요 없다.
기본값 수집에는 음성 코드의 기존 PC 의존성(`requirements-pc.txt`)이 필요하다.
관리 중에는 해당 DB를 편집하는 다른 작업을 멈춰 백업과 변경 사이에 쓰기가 끼지 않게 한다.

```powershell
# 신규 DB 생성. 기존 8테이블 DB는 위 migrate 명령부터 실행한다.
python voice_store.py --seed

# 기존 합성 DB를 읽기 전용으로 내보낸다. 파일 이름은 새 이름을 사용한다.
python db_transfer.py export --voice-db voice_data.db --demo --output demo.json
python db_transfer.py check demo.json

# 기존 파일과 분리해 먼저 내용 확인. 재실행하면 같은 자료는 추가되지 않는다.
python db_transfer.py import demo.json --db demo_preview.db
python db_transfer.py import demo.json --db demo_preview.db
```

`export`의 `--mes-db` 옵션은 가상 MES와 함께 폐기됐다. 가져오기 대상 DB가 있으면
변경 전에 자동 백업한다. 테이블·열·필수값·종류·명령·시나리오·시각을 먼저 검증하며,
실제 쓰기 중 오류가 나도 트랜잭션 전체를 취소한다. 메인 안전 이력 테이블은 입력 거부한다.
가상 MES 테이블이 든 이전 묶음은 조용히 버리지 않고 폐기 안내와 함께 거부한다.

기존 기본키가 있으면 **대상 DB의 값이 우선**한다. 이 명령은 동기화/덮어쓰기 도구가 아니다.
관리자의 DB 설정 변경은 `voice_store.py --set/--add-roster/--add-phrase`를 사용한다.
호출어 추가/삭제 `--add/--del`은 로컬 JSON에 적용되며 `--remote`와 함께 사용할 수 없다.

내보내기 파일과 SQL은 기존 파일을 덮어쓰지 않는다. DB·JSON·SQL 데이터 묶음은 로컬 전용이며
실제 직원/운영 자료를 이 합성 데모 도구에 넣지 않는다. 비밀값 계열 설정은 반출을 거부한다.

## Supabase로 가져오기

1. 대상이 **합성 데모용 프로젝트**인지 확인하고 `supabase_setup.sql`을 SQL Editor에서 실행한다.
2. 아래 명령으로 검토 가능한 SQL 파일을 만든다.
3. 생성한 파일을 같은 프로젝트의 SQL Editor에서 실행한다. 관리자 권한이 필요하다.

```powershell
python db_transfer.py sql demo.json --output demo_import.sql
# 음성 PC로 배포할 규칙 파일은 SQL과 별도다. 기존 파일을 덮어쓰지 않는다.
python db_transfer.py rules demo.json --output voice_data.rules.json
```

현재 내보내기 형식은 v2다. 음성3테이블 및 선택적 `rules` 객체를 담는다.
기존 v1 묶음도 읽으며 규칙 테이블을 JSON으로 변환한다(`factory_rules` 표는 버린다). SQLite 가져오기는
대상 DB 옆에 규칙 파일을 생성하고, 이미 있는 파일은 보존한다. Supabase SQL에는
운영 테이블만 들어가므로 rules 추출 파일을 음성 PC에 별도 배치한다.

생성 SQL은 `BEGIN/COMMIT` 한 트랜잭션이며 기존 키를 보존한다.
이 도구가 원격 DB에 접속하거나 자동 업로드하지는 않는다.
갱신시각은 원본 그대로 옮긴다. 같은 키를 자동으로 덮어쓰지는 않는다.

읽기 연결은 기존 `SUPABASE_URL`·`SUPABASE_ANON_KEY`를 사용한다.
원격 관리 쓰기에는 별도의 `SUPABASE_WRITE_KEY`가 필요하며 프런트엔드에 넣지 않는다.
읽기 전용 음성 PC에서는 원격 문구 수정 요청을 거부한다. 관리용 서버/CLI에서만 쓰기 키를 사용한다.
원격 REST 시드는 테이블별 요청이라 중간 실패 시 일부만 반영될 수 있고 종료 코드 1을 반환한다.
전체 원자성이 필요한 최초 가져오기는 위 SQL 경로를 사용한다.

제공 RLS는 합성 데모를 anon으로 읽는 정책이다. `settings`는 알려진 공개 설정만 노출한다.
실제 직원 명단은 그대로 공개하면 안 되므로 인증 사용자별 RLS를 별도 설계해야 한다.
기존 프로젝트에 다른 정책/권한이 있으면 이 SQL만으로 모두 제한된다고 보장하지 않는다.

`knowledge/`의 작업현황·출하스케줄은 날짜가 있는 합성 원문이며 DB로 변환하지 않는다.
구 `mes_demo.db`(15행)는 폐기됐고 가져오기 경로도 없다. 남아 있는 로컬 파일은 쓰이지 않는다.

## 검증

```powershell
python -m unittest discover -s . -p 'test_*.py'
```

`test_voice_migration.py`는 8→3 이전, 맞춤 규칙·빈 명단 보존, 백업, 실패·충돌 중단,
JSON 문구 단일 이전, v1 호환, 규칙 캐시/검증을 확인한다. `test_db_transfer.py`는 반복 가져오기, 기존 값/백업 보존, 마지막 행 오류의 전체 거부,
쓰기 실패 롤백, 메인 테이블·가상 MES 테이블 거부, 비밀 설정/잘못된 명령 거부, 명단 실패 시
승인 차단 등을 확인한다. 원격 실행 전에는 프로젝트의 실제 RLS·권한도 확인한다.

참고: [PostgREST 중복 무시](https://docs.postgrest.org/en/v14/references/api/tables_views.html#upsert),
[PGlite PostgreSQL 시험 API](https://pglite.dev/docs/api).
