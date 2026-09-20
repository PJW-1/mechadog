-- MechDog 음성 설정 + 가상 MES 스키마 for Supabase
-- 합성 데모 프로젝트용. 스키마만 생성/보완한다. 기존 행은 삭제/갱신하지 않는다.
-- 메인 안전 이력 robots/mission_runs/incidents/zones에는 접근하지 않는다.
-- 로컬 자료는 db_transfer.py export → sql로 별도 가져온다.
--
-- 읽기: anon 키에 select 정책만 연다 (음성 PC에는 anon 키만 둔다).
-- 쓰기: 정책을 만들지 않으므로 service_role 키로만 가능 — 관리 도구에만 둔다.
-- 시드: db_transfer.py의 SQL 또는 --seed --remote(기존 키 보존).
-- anon 읽기는 합성 데모 전용이다. 실제 직원/생산 자료는 별도 인증/RLS 설계 후 사용.

BEGIN;
SET LOCAL search_path = public;

-- ═══ voice_store 테이블 ═══════════════════════════════════════════════

-- 고정 규칙5개는 PC JSON으로 관리한다. 기존 원격 규칙 테이블은 자동 삭제하지 않는다.
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

-- ═══ 가상 MES 테이블 (factory_mes.py가 읽는 정본) ═══════════════════════

create table if not exists production_status (
    line_id text primary key,
    product text not null,
    target_quantity integer not null,
    completed_quantity integer not null,
    state text not null,              -- running | stopped | idle
    updated_at timestamptz not null default now()
);
create table if not exists shipment_schedule (
    shipment_id text primary key,
    customer text not null,
    product text not null,
    quantity integer not null,
    deadline date not null,
    dock text,
    updated_at timestamptz not null default now()
);
create table if not exists work_schedule (
    task_id text primary key,
    line_id text not null,
    description text not null,
    priority integer not null,        -- 1 = 가장 급함
    start_at timestamptz not null,
    deadline date not null,
    status text not null,             -- pending | in_progress | done
    updated_at timestamptz not null default now()
);
create table if not exists inspection_log (
    id bigint generated always as identity primary key,
    line_id text not null,
    lot text not null,
    inspected integer not null,
    defects integer not null,
    result text not null,             -- pass | fail | hold
    updated_at timestamptz not null default now()
);
create table if not exists equipment_check (
    id bigint generated always as identity primary key,
    equipment text not null,
    line_id text not null,
    check_item text not null,
    result text not null,             -- ok | warn | fail
    checked_at timestamptz not null,
    updated_at timestamptz not null default now()
);

-- 로컬 AUTOINCREMENT id를 원격 identity id로 복사하지 않는다.
-- 행 내용 해시를 가져오기 키로 사용해 재실행 시 이력을 중복 생성하지 않는다.
alter table inspection_log add column if not exists import_key text;
alter table equipment_check add column if not exists import_key text;
create unique index if not exists inspection_log_import_key on inspection_log(import_key);
create unique index if not exists equipment_check_import_key on equipment_check(import_key);
create index if not exists inspection_log_line_updated on inspection_log(line_id, updated_at desc);
create index if not exists equipment_check_equipment_checked on equipment_check(equipment, checked_at desc);

-- 기존 행은 보존하되 이후 입력에는 자료형/상태 제약을 적용한다.
-- NOT VALID: 기존 자료를 자동 정정/삭제하지 않는다. 검토 후 VALIDATE CONSTRAINT 가능.
do $checks$
declare item record;
begin
    for item in select * from (values
        ('production_status', 'demo_production_values', $$target_quantity >= 0 and completed_quantity >= 0 and state in ('running','stopped','idle')$$),
        ('shipment_schedule', 'demo_shipment_quantity', $$quantity >= 0$$),
        ('work_schedule', 'demo_work_values', $$priority >= 0 and status in ('pending','in_progress','done')$$),
        ('inspection_log', 'demo_inspection_values', $$inspected >= 0 and defects between 0 and inspected and result in ('pass','fail','hold')$$),
        ('equipment_check', 'demo_equipment_result', $$result in ('ok','warn','fail')$$)
    ) as checks(table_name, constraint_name, expression) loop
        if not exists (select 1 from pg_constraint where conname=item.constraint_name
                       and conrelid=format('public.%I', item.table_name)::regclass) then
            execute format('alter table public.%I add constraint %I check (%s) not valid',
                           item.table_name, item.constraint_name, item.expression);
        end if;
    end loop;
end $checks$;

-- ═══ RLS — anon 읽기만 허용 ═════════════════════════════════════════════

do $$
declare t text;
begin
    foreach t in array array[
        'settings','roster','phrases',
        'production_status','shipment_schedule','work_schedule',
        'inspection_log','equipment_check'
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
    key in ('follow_s', 'follow_min_chars', 'stt_prompt', 'machine_notice',
            'robot_api_base', 'mes_api_base')
);
COMMIT;
