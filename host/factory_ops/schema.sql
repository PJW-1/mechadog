-- 가상 MES 운영 데이터 스키마 (WBS 4.7.15 · FR-12.1 · FR-12.5 · ADR-34).
--
-- ⚠️ **이 파일이 정본이고 런타임 DB 는 생성물이다.** DB 파일은 커밋하지 않으며
-- `python tools/factory_ops_seed.py` 로 다시 만든다 (DoD ①).
--
-- ⚠️ **개인정보를 담을 칸 자체를 두지 않는다** (FR-12.5). 직원 이름·연락처·사번·
-- 암구호를 넣을 자리가 없으면 실수로 들어갈 수도 없다. 값을 검사하는 것보다
-- 칸을 없애는 쪽이 확실하며, `tests/test_factory_ops.py` 가 이것을 전수로 지킨다.
--
-- ⚠️ **네 표뿐이다** (ADR-34 데이터 범위). 사건 JSONL·블랙박스·순찰 리포트는 이미
-- 목적에 맞게 돌고 있으므로 SQLite 로 복제하지 않는다.
--
-- 모든 표에 `updated_at` 이 있다. 신선도 판정(`4.7.17`)이 이 칸 하나에 걸리므로
-- **시간대가 있는 ISO 8601 문자열**로 통일한다. 시간대를 빼면 같은 문자열이
-- 서버마다 다른 시각을 뜻하게 된다.

PRAGMA user_version = 1;

-- 라인별 생산 현황. 한 라인에 한 행이며 «지금 값»만 둔다.
CREATE TABLE IF NOT EXISTS production_status (
    line_id            TEXT PRIMARY KEY,
    product            TEXT    NOT NULL,
    target_quantity    INTEGER NOT NULL,
    completed_quantity INTEGER NOT NULL,
    state              TEXT    NOT NULL,  -- running | stopped | idle
    updated_at         TEXT    NOT NULL
);

-- 납품·출하 일정.
CREATE TABLE IF NOT EXISTS shipment_schedule (
    shipment_id TEXT PRIMARY KEY,
    customer    TEXT    NOT NULL,         -- 합성 거래처명. 실제 거래처가 아니다
    product     TEXT    NOT NULL,
    quantity    INTEGER NOT NULL,
    deadline    TEXT    NOT NULL,         -- ISO 날짜
    dock        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- 작업지시와 우선순위.
CREATE TABLE IF NOT EXISTS work_schedule (
    task_id     TEXT PRIMARY KEY,
    line_id     TEXT    NOT NULL,
    description TEXT    NOT NULL,         -- 할 일. 담당자를 적지 않는다 (FR-12.5)
    priority    INTEGER NOT NULL,         -- 1 이 가장 급하다
    start_at    TEXT    NOT NULL,         -- ISO 날짜
    deadline    TEXT    NOT NULL,         -- ISO 날짜
    status      TEXT    NOT NULL,         -- pending | in_progress | done
    updated_at  TEXT    NOT NULL
);

-- 설비 가동 상태.
--
-- ⚠️ **점검 이력이 아니라 «지금 상태» 다.** FR-12.1 이 묻는 것은 *"그 설비가 지금
-- 돌고 있는가"* 이고 점검 로그로는 그 질문에 답할 수 없다. 이력이 실제 요구가 되면
-- 그때 별도 표로 정한다.
--
-- ⚠️ **고장 원인을 적는 칸을 두지 않는다** (FR-12.5). 고장 질문은 진단이 아니라
-- 매뉴얼의 점검 절차 안내로 답해야 하고, 그 안내의 정본은 `knowledge/` 문서다.
-- 여기에 원인 칸을 두면 DB 가 진단을 말하는 길이 생긴다.
CREATE TABLE IF NOT EXISTS equipment_status (
    equipment_id TEXT PRIMARY KEY,
    label        TEXT NOT NULL,           -- 사람이 부르는 설비 이름
    line_id      TEXT NOT NULL,
    state        TEXT NOT NULL,           -- running | idle | maintenance | fault
    updated_at   TEXT NOT NULL
);
