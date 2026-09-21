-- 가상 MES 운영 테이블 — ADR-34 「데이터 범위」 (WBS 4.7.15 DoD ②)
--
-- 1차 구현은 이 **네 개**뿐이다. 사건 JSONL·블랙박스·순찰 리포트는 이미 목적에
-- 맞게 동작하므로 여기로 복제하지 않는다.
--
-- 모든 테이블에 `updated_at` 이 있다 — 신선도 계약(4.7.17)이 행마다 이 값을
-- 읽는다. 없는 행은 오래된 것으로 친다.

CREATE TABLE IF NOT EXISTS production_status (
    line_id            TEXT PRIMARY KEY,
    product            TEXT    NOT NULL,
    target_quantity    INTEGER NOT NULL,
    completed_quantity INTEGER NOT NULL,
    state              TEXT    NOT NULL,   -- running | stopped | idle
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_schedule (
    shipment_id TEXT PRIMARY KEY,
    customer    TEXT    NOT NULL,
    product     TEXT    NOT NULL,
    quantity    INTEGER NOT NULL,
    deadline    TEXT    NOT NULL,          -- ISO 날짜
    dock        TEXT,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS work_schedule (
    task_id     TEXT PRIMARY KEY,
    line_id     TEXT    NOT NULL,
    description TEXT    NOT NULL,
    priority    INTEGER NOT NULL,          -- 1 = 가장 급함
    start_at    TEXT    NOT NULL,
    deadline    TEXT    NOT NULL,
    status      TEXT    NOT NULL,          -- pending | in_progress | done
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment_status (
    equipment  TEXT PRIMARY KEY,
    line_id    TEXT NOT NULL,
    check_item TEXT NOT NULL,
    result     TEXT NOT NULL,              -- ok | warn | fail
    checked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
