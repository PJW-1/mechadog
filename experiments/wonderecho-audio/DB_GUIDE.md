# 음성 설정과 합성 MES 데이터 관리

음성 DB는 **8개에서 3개 테이블로 줄였다**. 운영자가 바꾸는 설정·명단·추가 응답은 DB,
고정 발화 규칙은 `voice_data.rules.json`으로 관리한다. PC에서 읽는 데이터이며
WonderEcho 펌웨어에 DB나 AI 모델을 넣지 않는다. 기능이나 명령 종류를 줄인 것이 아니다.

## 메인 안전 이력 DB와의 관계

사용자가 제공한 메인 ERD는 `robots → mission_runs → incidents`, `zones → incidents`로
누가·언제·어디서 순찰했고 어떤 안전 사건이 발생했는지 기록한다.
이번 도구는 이 네 테이블을 생성·수정하거나 가상 사건을 넣지 않는다.
`roster`의 이름을 `robot_id`에 연결하거나 MES 라인을 `zone_id`로 임의 연결하지도 않는다.
실제 연동은 운영상의 ID 대응과 이벤트 기록 계약이 정해진 뒤 별도로 구현한다.

| 영역 | 테이블/저장소 | 역할 |
| --- | --- | --- |
| 메인 안전 이력 | robots, mission_runs, incidents, zones | 로봇·순찰·안전 사건·구역 |
| 음성 운영 DB | settings, roster, phrases | 운영 설정·신원 명단·추가 응답 |
| 로컬 규칙 JSON | keywords, command_endings, action_commands, scenario_triggers, factory_rules | 호출어·명령·시나리오·MES 질의 분기 |
| 합성 MES | production_status, shipment_schedule, work_schedule, inspection_log, equipment_check | 생산·출하·작업·검사·설비 점검 샘플 |
| 문서 RAG | knowledge/*.txt | 규정·매뉴얼·합성 시나리오 원문 |

현재 실험 코드의 MES는 **5테이블**이다. WBS 4.7.15의 목표 `equipment_status` 등을 포함한
`factory_ops/` 설계와 동일한 구현으로 간주하지 않는다. 이번 수정만으로 4.7.15~18이나
현장지원 모드의 실기 검증을 완료했다고 표시하지 않는다. 사건 JSONL도 기존 경로를 유지한다.

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
파일은 `format_version: 1` 및 아래 5개 섹션 전체를 갖는다.

| JSON 섹션 | 형식·예시 | 의미 |
| --- | --- | --- |
| keywords | `{"wake": ["메카독"], ...}` | wake/sleep/resume/emergency/status/machine 단어 |
| command_endings | `["해주세요", "해줘", ...]` | 명령에서 벗겨낼 어미, 긴 것부터 적용 |
| action_commands | `{"순찰시작": ["patrol_start", "순찰을 시작합니다."]}` | 즉시 실행할 허용 명령과 확인 멘트 |
| scenario_triggers | `{"신원확인": "guard", ...}` | 기존 시나리오 시작 구문 |
| factory_rules | `[["생산량", "production", 0, 1, 10], ...]` | 키워드, API, 라인필수, 라인전달, 우선순위 |

규칙 자체는 필요하지만 이 목록마다 DB 테이블을 둘 필요는 없다. action과 scenario는
실행 방식이 달라 섹션을 구분하고 동일 구문 충돌을 검증한다. 위 예시는 부분 설명이며,
전체 파일은 `python voice_store.py --rules`로 확인한다.

`voice_rules.py`는 파일 변경 시에만 파싱/검증한다. 고정 규칙 조회에서는 SQLite·Supabase를
호출하지 않는다. 잘못된 JSON/미등록 action·scenario/API는 경고 후 기본 규칙을 사용한다.
CLI 저장은 검증 후 원자적으로 교체한다. 비상정지 3개 구문은 삭제해도 코드가 보완하며,
다른 action으로 재매핑하는 저장은 거부한다. 로봇 실행 화이트리스트와 런타임 게이트는 유지한다.
JSON 안의 빈 목록은 해당 규칙을 끄는 명시적 설정이다(비상정지 보호 구문 제외).

### 기존 8테이블 DB 이전

DB/설정 편집 프로세스를 종료한 뒤 실행한다. 서비스 중 자동 이전하지 않는다.

```powershell
python migrate_voice_db.py --db voice_data.db --check
python migrate_voice_db.py --db voice_data.db
```

검증 → SQLite 전체 백업 → 유효한 규칙 JSON 저장 → 5개 규칙 테이블 제거 순서다.
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
- MES는 읽기 전용 SQLite로 조회한다. 없는 DB를 조회하다 빈 파일을 만들지 않는다.
  잘못된 시각·시간대 없는 시각·미래 시각은 `stale=true`, `fresh=false`다.
  설비 최신 기록은 삽입 ID가 아니라 `checked_at`으로 고른다.
- `supabase_setup.sql`은 스키마·인덱스·RLS만 적용한다. 기존의 전체 MES
  `TRUNCATE`와 자동 재시드는 제거했다. 입력 제약은 기존 행을 지우지 않는
  `NOT VALID`로 추가한다. 기존 행의 정합성은 별도 감사/검증 대상이다.

## 로컬 데이터를 가져오기

음성 PC의 기존 Python 환경에서 이 폴더로 이동해 실행한다. GPU·로봇·모듈 연결은 필요 없다.
기본값 수집에는 음성 코드의 기존 PC 의존성(`requirements-pc.txt`)이 필요하다.
관리 중에는 해당 DB를 편집하는 다른 작업을 멈춰 백업과 변경 사이에 쓰기가 끼지 않게 한다.

```powershell
# 신규 DB 생성. 기존 8테이블 DB는 위 migrate 명령부터 실행한다.
python voice_store.py --seed

# 기존 합성 DB를 읽기 전용으로 내보낸다. 파일 이름은 새 이름을 사용한다.
python db_transfer.py export --voice-db voice_data.db --mes-db mes_demo.db --demo --output demo.json
python db_transfer.py check demo.json

# 기존 파일과 분리해 먼저 내용 확인. 재실행하면 같은 자료는 추가되지 않는다.
python db_transfer.py import demo.json --db demo_preview.db
python db_transfer.py import demo.json --db demo_preview.db
```

`--voice-db`와 `--mes-db` 중 필요한 것만 지정해도 된다. 가져오기 대상 DB가 있으면
변경 전에 자동 백업한다. 테이블·열·필수값·종류·명령·상태·수량·시각을 먼저 검증하며,
실제 쓰기 중 오류가 나도 트랜잭션 전체를 취소한다. 메인 안전 이력 테이블은 입력 거부한다.

기존 기본키가 있으면 **대상 DB의 값이 우선**한다. 이 명령은 동기화/덮어쓰기 도구가 아니다.
관리자의 DB 설정 변경은 `voice_store.py --set/--add-roster/--add-phrase`를 사용한다.
호출어 추가/삭제 `--add/--del`은 로컬 JSON에 적용되며 `--remote`와 함께 사용할 수 없다.
검사·설비 이력의 로컬 숫자 ID는 복사하지 않는다. 대상 DB가 ID를 발급하고,
`import_key`에 행 내용 해시를 기록한다. 같은 자료를 다시 넣어도 이력이 중복되지 않는다.
이전 버전이 넣은 동일 내용의 이력도 확인하고 건너뛴다.

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

현재 내보내기 형식은 v2다. 음성3+MES5 테이블 및 선택적 `rules` 객체를 담는다.
기존 v1 묶음도 읽으며 5개 규칙 테이블을 JSON으로 변환한다. SQLite 가져오기는
대상 DB 옆에 규칙 파일을 생성하고, 이미 있는 파일은 보존한다. Supabase SQL에는
운영 테이블만 들어가므로 rules 추출 파일을 음성 PC에 별도 배치한다.

생성 SQL은 `BEGIN/COMMIT` 한 트랜잭션이며 기존 키 보존·이력 중복 방지를 적용한다.
이 도구가 원격 DB에 접속하거나 자동 업로드하지는 않는다.
숫자·갱신시각은 원본 그대로이므로 **오래된 데모를 가져왔다고 최신 자료가 되지 않는다**.
새 날짜의 합성 자료가 필요하면 새 경로에 `factory_mes.py --db 새이름.db --seed`로 만들고,
기존 자료와 구분해 내보낸다. 같은 업무 키를 자동으로 덮어쓰지는 않는다.

읽기 연결은 기존 `SUPABASE_URL`·`SUPABASE_ANON_KEY`, MES 서버는
`python factory_mes.py --serve --backend supabase`를 사용한다.
원격 관리 쓰기에는 별도의 `SUPABASE_WRITE_KEY`가 필요하며 프런트엔드에 넣지 않는다.
읽기 전용 음성 PC에서는 원격 문구 수정 요청을 거부한다. 관리용 서버/CLI에서만 쓰기 키를 사용한다.
원격 REST 시드는 테이블별 요청이라 중간 실패 시 일부만 반영될 수 있고 종료 코드 1을 반환한다.
전체 원자성이 필요한 최초 가져오기는 위 SQL 경로를 사용한다.

제공 RLS는 합성 데모를 anon으로 읽는 정책이다. `settings`는 알려진 공개 설정만 노출한다.
실제 직원 명단·공장 데이터는 그대로 공개하면 안 되므로 인증 사용자별 RLS를 별도 설계해야 한다.
기존 프로젝트에 다른 정책/권한이 있으면 이 SQL만으로 모두 제한된다고 보장하지 않는다.

문서 RAG의 작업현황·출하스케줄은 날짜가 있는 합성 원문이며 DB의 실시간 운영값으로
자동 변환하지 않는다. 기존 `mes_demo.db`의 구조화된 15행을 가져오는 경로와 구분한다.

## 검증

```powershell
python -m unittest discover -s . -p 'test_*.py'
```

`test_voice_migration.py`는 8→3 이전, 맞춤 규칙·빈 명단 보존, 백업, 실패·충돌 중단,
JSON 문구 단일 이전, v1 호환, 규칙 캐시/검증을 확인한다. `test_db_transfer.py`는 반복 가져오기, 기존 값/백업 보존, 마지막 행 오류의 전체 거부,
쓰기 실패 롤백, 메인 테이블 거부, 비밀 설정/잘못된 명령 거부, 명단 실패 시 승인 차단,
신선도와 삽입 순서 역전 등을 확인한다. 원격 실행 전에는 프로젝트의 실제 RLS·권한도 확인한다.

참고: [PostgREST 중복 무시](https://docs.postgrest.org/en/v14/references/api/tables_views.html#upsert),
[PGlite PostgreSQL 시험 API](https://pglite.dev/docs/api).
