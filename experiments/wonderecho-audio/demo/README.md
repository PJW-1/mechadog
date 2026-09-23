# 팀 공유용 음성 합성 데이터

`voice_demo.json`에 기존 로컬 DB의 **음성3테이블 10행**과 **발화 규칙 111개**를
담았다. 직원명은 합성 데모이며 실제 회사의 운영 자료가 아니다.
SQLite 실행 파일은 생성물로 두고 JSON·스키마·가져오기 SQL을 버전 관리한다.
음성 DB 정본은 3테이블이다.

| 데이터 | 파일/테이블 | 개수 |
| --- | --- | --- |
| 운영 설정 | settings | 4행 |
| 가상 직원 명단 | roster | 6행 |
| 관리자 추가 문구 | phrases | 0행 |
| 호출어·어미·명령·시나리오 규칙 | JSON rules | 111개 |

추가 문구가 0행이어도 [phrases.py](../phrases.py)의 기본 응답 121개는 사용된다.
안전 규정·매뉴얼·회사 Q&A 합성 문서 25개는 [knowledge/](../knowledge/)에 함께 있다.
문서는 원문이 정본이므로 임의로 DB에 중복 적재하지 않는다.

## 내려받아서 같은 DB 만들기

저장소를 클론한 후 `dev`에서 실행한다.

```powershell
git clone https://github.com/PJW-1/mechadog.git
cd mechadog
git switch dev
cd experiments/wonderecho-audio

# Python 3.12. DB 생성만 할 때는 GPU·모델·로봇 연결·추가 pip 설치가 필요 없다.
python prepare_demo.py --output-dir .
```

현재 폴더에 다음 파일이 만들어진다.

```text
voice_data.db          # settings / roster / phrases
voice_data.rules.json  # 고정 발화 규칙 4종
```

**이미 있는 DB·규칙 파일은 덮어쓰지 않는다.** 다시 실행해도 삭제한 직원이 재등록되지 않는다.
기존 8테이블 음성 DB가 있으면 먼저 `python migrate_voice_db.py --db voice_data.db`로 이전한다.
기존 데이터와 명시적으로 합칠 때는 [DB_GUIDE.md](../DB_GUIDE.md)의 `db_transfer.py import`를
사용한다. 이 명령은 기존 키를 보존하면서 없는 행을 추가하고, 변경 전 DB를 백업한다.
신규 DB 생성은 하나의 트랜잭션이다.

## 실제 들어 있는 값 확인

```powershell
python db_transfer.py check demo/voice_demo.json
python voice_store.py --dump
python voice_store.py --check-schema
python voice_store.py --rules
```

## Supabase에 같은 내용 가져오기

1. 합성 데모용 프로젝트의 SQL Editor에서 [supabase_setup.sql](../supabase_setup.sql)을 실행한다.
2. 이어서 [import_supabase.sql](import_supabase.sql)을 실행한다.
3. 음성 PC에서는 위 `prepare_demo.py`로 규칙 JSON을 준비하고, 원격 연결 환경변수는
   [DB_GUIDE.md](../DB_GUIDE.md)의 안내에 따라 설정한다.

SQL은 음성3 테이블만 대상으로 한다. 메인 안전 이력의
`robots / mission_runs / incidents / zones`는 생성·수정하지 않는다.
재실행 시 기존 키의 행을 보존한다.
고정 발화 규칙은 PC JSON에 있으므로 Supabase에 규칙 테이블을 다시 만들지 않는다.
이전 SQL로 만든 가상 MES 테이블(2026-09-23 폐기)은 자동 삭제하지 않으므로 필요하면 직접 정리한다.
원격 구버전에서 바꾼 규칙은 별도 내보내기 후 이전해야 한다.

제공 SQL의 anon 읽기 정책은 합성 데모용이다. 실제 직원 자료를 넣는 운영 DB에서는
인증/RLS를 별도로 설계한다. 이 PR은 실제 Supabase에 업로드하지 않는다.

## 음성모듈까지 실행하려면

- PC 모델·GPU 환경: [SETUP.md](../SETUP.md)
- 발화 분기·시나리오: [VOICE_ROUTING.md](../VOICE_ROUTING.md)
- 모듈 플래시와 실제 청취 확인: [TESTING.md](../TESTING.md)

데이터 설치는 모듈 플래시와 별개다. `voice_pipeline.py`는 현재 별도 PC 프로세스이며
Host 런타임이 자동으로 실행하지 않는다. PC에서 인식·데이터 조회·합성을 수행하고,
모듈은 마이크/스피커 입출력을 맡는다. 현재 USB(COM) 전송은 임시 경로다. 로봇 4핀→ESP32→Wi-Fi
오디오 중계(WBS 4.7.9)는 2026-09-23 실측으로 불가 판정됐고, 목표 구조는 듣기를 XIAO `:82`(4.7.19),
말하기를 로봇 MP3 모듈 `0x7B`(4.7.20·4.7.21)가 맡는다([ADR-38](../../../docs/DECISIONS.md#adr-38)).

고정 안내음 제작에서 선택한 Orpheus 준서(seed7)와 실시간 대화 예제의 Piper TTS는
서로 다른 합성 경로다. 모델 가중치·음성 캐시·사용자 녹음·벤더 SDK·펌웨어 BIN은
이 데이터 묶음에 포함하지 않는다. PC 설치와 실물에서 소리가 나는지는 별도로 검증한다.

## 검증과 갱신

```powershell
python -m unittest discover -s . -p 'test_*.py'
```

`test_prepare_demo.py`는 실제 게시 JSON/SQL의 일치, 음성 DB 재현, 원본 날짜 보존,
재실행 시 설정/삭제된 명단 보존, 잘못된 입력의 사전 거부를 검증한다.
JSON을 고쳤다면 다음 명령으로 SQL을 새 파일에 생성해 내용을 검토한 뒤 함께 갱신한다.

```powershell
python db_transfer.py sql demo/voice_demo.json --output demo/import_supabase.next.sql
```

운영 DB·`.env`·키·토큰·암구호·개인 녹음·로컬 백업을 이 폴더에 복사하지 않는다.
