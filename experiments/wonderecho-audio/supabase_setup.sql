-- MechDog 음성 설정 + 가상 MES 스키마 for Supabase
-- Supabase 프로젝트 생성 후 SQL 에디터에 이 파일을 그대로 실행한다.
--
-- 읽기: anon 키에 select 정책만 연다 (음성 PC에는 anon 키만 둔다).
-- 쓰기: 정책을 만들지 않으므로 service_role 키로만 가능 — 관리 도구에만 둔다.
-- 시드: `python voice_store.py --seed --remote` 가 코드 기본값을 업서트한다.

-- ═══ voice_store 테이블 ═══════════════════════════════════════════════

create table if not exists keywords (
    kind text not null,               -- wake|sleep|resume|emergency|status|machine
    word text not null,
    primary key (kind, word)
);
create table if not exists command_endings (
    ending text primary key           -- 벗겨낼 명령 어미
);
create table if not exists action_commands (
    phrase text primary key,
    action text not null,             -- estop|manual_on|manual_off|patrol_start|patrol_stop
    ack text not null                 -- 실행 후 읽는 확인 멘트
);
create table if not exists scenario_triggers (
    phrase text primary key,
    scenario text not null            -- scenarios.SCENARIOS 키만 허용(코드가 검증)
);
create table if not exists factory_rules (
    keyword text primary key,
    endpoint text not null,           -- production|shipments|schedule|inspections|equipment
    needs_line boolean not null default false,
    attach_line boolean not null default false,
    priority integer not null default 100
);
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

-- ═══ RLS — anon 읽기만 허용 ═════════════════════════════════════════════

do $$
declare t text;
begin
    foreach t in array array[
        'keywords','command_endings','action_commands','scenario_triggers',
        'factory_rules','settings','roster','phrases',
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

-- ═══ 데모 MES 데이터 (로컬 seed()와 같은 스토리) ════════════════════════
-- ID는 데이터 날짜에서 유도 — 어느 날 실행해도 ID↔날짜가 어긋나지 않는다.

truncate production_status, shipment_schedule, work_schedule,
         inspection_log, equipment_check;

insert into production_status values
    ('A', 'MD-100 구동모듈', 1200, 780, 'running', now()),
    ('B', 'MD-200 센서모듈', 800, 800, 'idle', now()),
    ('C', 'MD-100 구동모듈', 600, 210, 'stopped', now());

insert into shipment_schedule
    (shipment_id, customer, product, quantity, deadline, dock, updated_at) values
    ('SH-' || to_char(current_date + 1, 'MMDD') || '-01', '한국정밀', 'MD-100 구동모듈', 400, current_date + 1, '2번 도크', now()),
    ('SH-' || to_char(current_date + 3, 'MMDD') || '-02', '대성산업', 'MD-200 센서모듈', 300, current_date + 3, '1번 도크', now()),
    ('SH-' || to_char(current_date + 5, 'MMDD') || '-03', '한국정밀', 'MD-100 구동모듈', 600, current_date + 5, '미정', now());

insert into work_schedule values
    ('WO-1001', 'A', 'MD-100 구동모듈 잔량 생산', 1, now(), current_date + 1, 'in_progress', now()),
    ('WO-1002', 'C', 'C라인 정지 원인 점검 후 재가동', 2, now(), current_date + 2, 'pending', now()),
    ('WO-1003', 'B', 'MD-200 후속 물량 준비', 3, now(), current_date + 4, 'pending', now());

insert into inspection_log (line_id, lot, inspected, defects, result, updated_at) values
    ('A', 'LOT-A' || to_char(current_date, 'MMDD'), 200, 3, 'pass', now()),
    ('B', 'LOT-B' || to_char(current_date - 1, 'MMDD'), 300, 0, 'pass', now()),
    ('C', 'LOT-C' || to_char(current_date, 'MMDD'), 80, 12, 'hold', now());

insert into equipment_check (equipment, line_id, check_item, result, checked_at, updated_at) values
    ('프레스-01', 'A', '유압·안전센서', 'ok', now(), now()),
    ('컨베이어-03', 'C', '벨트 장력', 'warn', now(), now()),
    ('로딩로봇-01', 'B', '그리퍼 캘리브레이션', 'ok', now(), now());
