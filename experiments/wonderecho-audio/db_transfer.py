"""Validated voice/MES demo transfer: SQLite -> JSON -> SQLite or Supabase SQL.

No network writes. SQL is a single transaction and never deletes/overwrites rows.
The safety-history tables (robots/mission_runs/incidents/zones) are out of scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import factory_mes
import voice_store
from sqlite_admin import backup_database, open_readonly

# Explicit column lists are also the import boundary. No SQL identifier comes from a file.
VOICE = {
    "keywords": ("kind", "word"),
    "settings": ("key", "value", "updated_at"),
    "roster": ("name", "note"),
    "action_commands": ("phrase", "action", "ack"),
    "factory_rules": ("keyword", "endpoint", "needs_line", "attach_line", "priority"),
    "phrases": ("category", "phrase"),
    "command_endings": ("ending",),
    "scenario_triggers": ("phrase", "scenario"),
}
MES = {
    "production_status": (
        "line_id",
        "product",
        "target_quantity",
        "completed_quantity",
        "state",
        "updated_at",
    ),
    "shipment_schedule": (
        "shipment_id",
        "customer",
        "product",
        "quantity",
        "deadline",
        "dock",
        "updated_at",
    ),
    "work_schedule": (
        "task_id",
        "line_id",
        "description",
        "priority",
        "start_at",
        "deadline",
        "status",
        "updated_at",
    ),
    "inspection_log": ("line_id", "lot", "inspected", "defects", "result", "updated_at"),
    "equipment_check": ("equipment", "line_id", "check_item", "result", "checked_at", "updated_at"),
}
HISTORY = ("inspection_log", "equipment_check")
TABLES = {**VOICE, **MES}
KEYS = {t: cols[:1] for t, cols in TABLES.items()}
KEYS.update(keywords=("kind", "word"), phrases=("category", "phrase"))
ENDPOINTS = {"production", "shipments", "schedule", "inspections", "equipment"}


def _digest(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _timestamp(value):
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("시간대가 없는 시각")
    return ts


def _validate_row(table, row):
    if not isinstance(row, dict) or set(row) != set(TABLES[table]):
        raise ValueError("컬럼 불일치")
    integers = {
        "priority",
        "target_quantity",
        "completed_quantity",
        "quantity",
        "inspected",
        "defects",
    }
    for col, value in row.items():
        if col in ("needs_line", "attach_line"):
            if type(value) not in (int, bool) or value not in (0, 1):
                raise ValueError("불리언은 0/1/true/false만 허용")
            row[col] = bool(value)
        elif col in integers:
            if type(value) is not int or value < 0 or value > 2147483647:
                raise ValueError("수량/우선순위는 0 이상의 32비트 정수")
        elif value is None and col in ("dock", "note"):
            continue
        elif not isinstance(value, str) or "\x00" in value:
            raise ValueError("문자열 형식 오류")
        elif not value.strip() and col not in ("note", "dock", "value"):
            raise ValueError("빈 문자열")
        if col in ("updated_at", "start_at", "checked_at"):
            _timestamp(value)
        if col == "deadline":
            date.fromisoformat(value)
    if table == "keywords" and row["kind"] not in voice_store._KEYWORD_KINDS:
        raise ValueError("알 수 없는 keyword kind")
    if table == "action_commands":
        import robotlink

        if row["action"] not in robotlink._ENDPOINTS:
            raise ValueError("허용되지 않은 action")
        if row["phrase"] in voice_store.PROTECTED_ACTIONS and row["action"] != "estop":
            raise ValueError("비상정지 재매핑 금지")
    if table == "scenario_triggers":
        import scenarios

        if row["scenario"] not in scenarios.SCENARIOS:
            raise ValueError("알 수 없는 scenario")
    if table == "factory_rules" and row["endpoint"] not in ENDPOINTS:
        raise ValueError("허용되지 않은 MES endpoint")
    if table == "phrases" and not re.fullmatch(r"[a-z0-9_]+", row["category"]):
        raise ValueError("문구 카테고리 형식 오류")
    if table == "settings":
        key, value = row["key"], row["value"]
        if voice_store._SENSITIVE_KEY_RE.search(key):
            raise ValueError("비밀 설정은 내보내기/가져오기 금지: 환경변수 사용")
        if key in ("follow_s", "follow_min_chars"):
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("음성 설정 수치는 유한한 0 이상 값")
            if key == "follow_min_chars":
                int(value)
        if key.endswith("_api_base"):
            url = urlsplit(value)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("API 주소는 인증정보/쿼리 없는 HTTP(S) 주소")
    enums = {
        "production_status": ("state", {"running", "stopped", "idle"}),
        "work_schedule": ("status", {"pending", "in_progress", "done"}),
        "inspection_log": ("result", {"pass", "fail", "hold"}),
        "equipment_check": ("result", {"ok", "warn", "fail"}),
    }
    if table in enums:
        col, allowed = enums[table]
        if row[col] not in allowed:
            raise ValueError("알 수 없는 상태")
    if table == "inspection_log" and row["defects"] > row["inspected"]:
        raise ValueError("불량 수가 검사 수보다 큼")


def validate(bundle):
    """Validate every row before opening a target DB; errors never include values."""
    if not isinstance(bundle, dict) or bundle.get("format_version") != 1:
        raise ValueError("지원하지 않는 묶음 버전")
    if bundle.get("data_kind") != "synthetic-demo":
        raise ValueError("이 도구는 합성 데모 데이터 전용")
    tables = bundle.get("tables")
    if not isinstance(tables, dict) or not tables or set(tables) - TABLES.keys():
        raise ValueError("지원하지 않는 테이블: 메인 안전 이력 DB는 대상이 아닙니다")
    for table, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError(f"{table}: 행 목록이 아님")
        seen = set()
        for index, row in enumerate(rows, 1):
            try:
                _validate_row(table, row)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    f"{table} row {index}: 잘못된 값/컬럼 (내용은 출력하지 않음)"
                ) from exc
            key = _digest(row) if table in HISTORY else tuple(row[c] for c in KEYS[table])
            if key in seen:
                raise ValueError(f"{table} row {index}: 중복 키")
            seen.add(key)
    # Exact duplicate routes are ambiguous because actions precede scenarios in the pipeline.
    actions = {r["phrase"] for r in tables.get("action_commands", [])}
    if actions & {r["phrase"] for r in tables.get("scenario_triggers", [])}:
        raise ValueError("action_commands와 scenario_triggers에 같은 구문이 있음")
    return bundle


def export_bundle(voice_db=None, mes_db=None):
    tables = {}
    sources = []
    for path, spec in ((voice_db, VOICE), (mes_db, MES)):
        if path is None:
            continue
        conn = open_readonly(path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")  # one consistent snapshot across tables
            for table, cols in spec.items():
                names = ",".join(f'"{c}"' for c in cols)
                rows = [dict(r) for r in conn.execute(f'SELECT {names} FROM "{table}"')]
                tables[table] = rows
            sources.append({"file": Path(path).name, "tables": list(spec)})
        finally:
            conn.close()
    return validate(
        {
            "format_version": 1,
            "data_kind": "synthetic-demo",
            "exported_at": datetime.now(UTC).isoformat(),
            "sources": sources,
            "tables": tables,
        }
    )


def _ensure_history_keys(conn):
    for table in HISTORY:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if "import_key" not in cols:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN import_key TEXT')
        conn.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_import_key" ON "{table}"(import_key)'
        )


def import_sqlite(bundle, target):
    """Merge into a local DB. Existing business keys win; bad rows roll back together."""
    validate(bundle)
    backup = backup_database(target)
    conn = sqlite3.connect(target)
    counts = {}
    try:
        # executescript would commit a pending transaction: split this trusted DDL instead.
        conn.execute("BEGIN IMMEDIATE")
        ddl = ""
        if set(bundle["tables"]) & VOICE.keys():
            ddl += voice_store.SCHEMA
        if set(bundle["tables"]) & MES.keys():
            ddl += factory_mes.SCHEMA
        for statement in ddl.split(";"):
            if statement.strip():
                conn.execute(statement)
        if set(bundle["tables"]) & MES.keys():
            _ensure_history_keys(conn)
        for table, rows in bundle["tables"].items():
            inserted = 0
            cols = TABLES[table]
            for row in rows:
                values = [row[c] for c in cols]
                write_cols = cols
                if table in HISTORY:
                    where = " AND ".join(f'"{c}" IS ?' for c in cols)
                    if conn.execute(
                        f'SELECT 1 FROM "{table}" WHERE {where} LIMIT 1', values
                    ).fetchone():
                        continue  # also recognize rows imported before import_key existed
                    write_cols = (*cols, "import_key")
                    values.append(_digest(row))
                names = ",".join(f'"{c}"' for c in write_cols)
                marks = ",".join("?" for _ in write_cols)
                cursor = conn.execute(
                    f'INSERT INTO "{table}" ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING',
                    values,
                )
                inserted += cursor.rowcount
            counts[table] = {"inserted": inserted, "kept": len(rows) - inserted}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"backup": str(backup) if backup else None, "tables": counts}


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def supabase_sql(bundle):
    """Generate reviewable transactional SQL; retain timestamps and target identity IDs."""
    validate(bundle)
    lines = [
        "-- Synthetic demo data ONLY. Run supabase_setup.sql first.",
        "-- Existing keys win. No main safety-history tables are modified.",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    for table, rows in bundle["tables"].items():
        cols = TABLES[table]
        for row in rows:
            values = [_literal(row[c]) for c in cols]
            write_cols = cols
            suffix = "ON CONFLICT DO NOTHING;"
            if table in HISTORY:
                where = " AND ".join(
                    f'"{c}" IS NOT DISTINCT FROM {v}' for c, v in zip(cols, values, strict=True)
                )
                write_cols = (*cols, "import_key")
                values.append(_literal(_digest(row)))
                source = f'SELECT {", ".join(values)} WHERE NOT EXISTS (SELECT 1 FROM public."{table}" WHERE {where})'
            else:
                source = f"VALUES ({', '.join(values)})"
            names = ", ".join(f'"{c}"' for c in write_cols)
            lines.append(f'INSERT INTO public."{table}" ({names}) {source} {suffix}')
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _write_new(path, text):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        out.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="합성 DB를 읽기 전용으로 내보내기")
    export.add_argument("--voice-db", type=Path)
    export.add_argument("--mes-db", type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument(
        "--demo",
        action="store_true",
        required=True,
        help="입력이 실제 직원/생산 자료가 아닌 합성 데이터임을 표시",
    )
    for name in ("check", "sql", "import"):
        sub = commands.add_parser(name)
        sub.add_argument("bundle", type=Path)
        if name == "sql":
            sub.add_argument("--output", required=True, type=Path)
        if name == "import":
            sub.add_argument("--db", required=True, type=Path)
    args = ap.parse_args()
    try:
        if args.command == "export":
            bundle = export_bundle(args.voice_db, args.mes_db)
            _write_new(args.output, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        else:
            bundle = validate(json.loads(args.bundle.read_text(encoding="utf-8-sig")))
            if args.command == "sql":
                _write_new(args.output, supabase_sql(bundle))
            elif args.command == "import":
                print(json.dumps(import_sqlite(bundle, args.db), ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "data_kind": bundle["data_kind"],
                    "rows": {t: len(rs) for t, rs in bundle["tables"].items()},
                },
                ensure_ascii=False,
            )
        )
    except ValueError as exc:
        ap.exit(1, f"[db-transfer] {exc}\n")
    except (OSError, sqlite3.Error) as exc:
        # Do not echo input data/credentials via exception messages from validators/drivers.
        ap.exit(
            1,
            f"[db-transfer] failed ({type(exc).__name__}); 파일·스키마·check 결과를 확인하세요.\n",
        )


if __name__ == "__main__":
    main()
