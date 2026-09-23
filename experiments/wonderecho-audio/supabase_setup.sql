-- MechDog 음성 설정 스키마 for Supabase
-- 합성 데모 프로젝트용. 스키마만 생성/보완한다. 기존 행은 삭제/갱신하지 않는다.
-- 메인 안전 이력 robots/mission_runs/incidents/zones에는 접근하지 않는다.
-- 로컬 자료는 db_transfer.py export → sql로 별도 가져온다.
--
-- 읽기: anon 키에 select 정책만 연다 (음성 PC에는 anon 키만 둔다).
-- 쓰기: 정책을 만들지 않으므로 service_role 키로만 가능 — 관리 도구에만 둔다.
-- 시드: db_transfer.py의 SQL 또는 --seed --remote(기존 키 보존).
-- anon 읽기는 합성 데모 전용이다. 실제 직원 자료는 별도 인증/RLS 설계 후 사용.

BEGIN;
SET LOCAL search_path = public;

-- ═══ voice_store 테이블 ═══════════════════════════════════════════════

-- 고정 규칙4종은 PC JSON으로 관리한다. 기존 원격 규칙·가상 MES(폐기) 테이블은 자동 삭제하지 않는다.
create table if not exists settings (
    key text primary key,
    value text not null,
    updated_at timestamptz not null default now()
);
create table if not exists roster (
    name text primary key,            -- 신원 확인 직원 명단 (scenarios.sc_guard)
    note text default ''
);
create table if not exists phrases (
    category text not null,           -- phrases.PHRASES 카테고리 키
    phrase text not null,
    primary key (category, phrase)
);

-- ═══ RLS — anon 읽기만 허용 ═════════════════════════════════════════════

do $$
declare t text;
begin
    foreach t in array array[
        'settings','roster','phrases'
    ] loop
        execute format('alter table %I enable row level security', t);
        execute format('drop policy if exists "anon read" on %I', t);
        execute format(
            'create policy "anon read" on %I for select to anon using (true)', t
        );
    end loop;
end $$;

-- settings를 비밀 저장소로 사용하지 않는다. 기존 포괄 정책을 알려진 공개 설정으로 제한.
drop policy if exists "anon read" on settings;
create policy "anon read" on settings for select to anon using (
    key in ('follow_s', 'follow_min_chars', 'stt_prompt', 'robot_api_base')
);
COMMIT;
