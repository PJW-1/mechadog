"""Validated voice demo transfer: SQLite -> JSON -> SQLite or Supabase SQL.

No network writes. SQL is a single transaction and never deletes/overwrites rows.
The safety-history tables (robots/mission_runs/incidents/zones) are out of scope.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import voice_rules
import voice_schema
import voice_store
from sqlite_admin import backup_database, open_readonly

# Explicit column lists are also the import boundary. No SQL identifier comes from a file.
VOICE = voice_schema.COLUMNS
# 2026-09-23 폐기(ADR-38): 가상 MES. 이전 묶음에 섞여 있으면 조용히 버리지 않고 거부한다.
RETIRED = (
    "production_status",
    "shipment_schedule",
    "work_schedule",
    "inspection_log",
    "equipment_check",
)
TABLES = VOICE
KEYS = {t: cols[:1] for t, cols in {**TABLES, **voice_rules.LEGACY}.items()}
KEYS.update(keywords=("kind", "word"), phrases=("category", "phrase"))


def _timestamp(value):
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("시간대가 없는 시각")
    return ts


def _validate_row(table, row):
    if not isinstance(row, dict) or set(row) != set({**TABLES, **voice_rules.LEGACY}[table]):
        raise ValueError("컬럼 불일치")
    for col, value in row.items():
        if value is None and col == "note":
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("문자열 형식 오류")
        if not value.strip() and col not in ("note", "value"):
            raise ValueError("빈 문자열")
        if col == "updated_at":
            _timestamp(value)
    # 구형 표의 폐기 호출어·시나리오 행은 voice_rules.from_legacy가 걸러 낸다(ADR-38).
    if table == "keywords" and row["kind"] not in (
        *voice_store._KEYWORD_KINDS,
        *voice_rules.RETIRED_KINDS,
    ):
        raise ValueError("알 수 없는 keyword kind")
    if table == "action_commands":
        import robotlink

        if row["action"] not in robotlink._ENDPOINTS:
            raise ValueError("허용되지 않은 action")
        if row["phrase"] in voice_store.PROTECTED_ACTIONS and row["action"] != "estop":
            raise ValueError("비상정지 재매핑 금지")
    if table == "scenario_triggers":
        import scenarios

        if row["scenario"] not in (*scenarios.SCENARIOS, *voice_rules.RETIRED_SCENARIOS):
            raise ValueError("알 수 없는 scenario")
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


def validate(bundle):
    """Validate every row before opening a target DB; errors never include values."""
    if not isinstance(bundle, dict) or bundle.get("format_version") not in (1, 2):
        raise ValueError("지원하지 않는 묶음 버전")
    if bundle.get("data_kind") != "synthetic-demo":
        raise ValueError("이 도구는 합성 데모 데이터 전용")
    bundle = copy.deepcopy(bundle)
    tables = bundle.get("tables")
    if isinstance(tables, dict) and tables.keys() & set(RETIRED):
        raise ValueError(
            "가상 MES 테이블은 폐기됨(ADR-38): 묶음의 tables에서 "
            + ", ".join(t for t in RETIRED if t in tables)
            + "를 지운 뒤 다시 실행하세요"
        )
    allowed = {**TABLES, **voice_rules.LEGACY} if bundle["format_version"] == 1 else TABLES
    if not isinstance(tables, dict) or not tables or set(tables) - allowed.keys():
        raise ValueError("지원하지 않는 테이블: 메인 안전 이력 DB는 대상이 아닙니다")
    if bundle["format_version"] == 1:
        # 구형 factory_rules 표는 voice_rules.from_legacy도 옮기지 않는다(ADR-38).
        tables.pop("factory_rules", None)
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
            key = tuple(row[c] for c in KEYS[table])
            if key in seen:
                raise ValueError(f"{table} row {index}: 중복 키")
            seen.add(key)
    # Exact duplicate routes are ambiguous because actions precede scenarios in the pipeline.
    actions = {r["phrase"] for r in tables.get("action_commands", [])}
    if actions & {r["phrase"] for r in tables.get("scenario_triggers", [])}:
        raise ValueError("action_commands와 scenario_triggers에 같은 구문이 있음")
    if bundle["format_version"] == 1:
        if set(tables) & voice_rules.LEGACY.keys():
            bundle["rules"] = voice_rules.from_legacy(tables)
            for table in voice_rules.LEGACY:
                tables.pop(table, None)
        bundle["format_version"] = 2
    if "rules" in bundle:
        # 이전 묶음의 폐기 규칙 섹션은 voice_rules.read()와 같이 걸러 낸다(ADR-38).
        bundle["rules"] = voice_rules.validate(voice_rules.drop_retired(bundle["rules"]))
    return bundle


def export_bundle(voice_db):
    tables = {}
    conn = open_readonly(voice_db)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # one consistent snapshot across tables
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names & voice_rules.LEGACY.keys():
            raise ValueError("먼저 migrate_voice_db.py --db <음성DB>로 이전하세요")
        path_rules = voice_rules.path_for(voice_db)
        rules = voice_rules.read(path_rules) if path_rules.exists() else voice_rules.defaults()
        for table, cols in VOICE.items():
            names = ",".join(f'"{c}"' for c in cols)
            tables[table] = [dict(r) for r in conn.execute(f'SELECT {names} FROM "{table}"')]
    finally:
        conn.close()
    return validate(
        {
            "format_version": 2,
            "data_kind": "synthetic-demo",
            "exported_at": datetime.now(UTC).isoformat(),
            "sources": [{"file": Path(voice_db).name, "tables": list(VOICE)}],
            "tables": tables,
            "rules": rules,
        }
    )


def import_sqlite(bundle, target):
    """Merge into a local DB. Existing business keys win; bad rows roll back together."""
    bundle = validate(bundle)
    backup = backup_database(target)
    conn = sqlite3.connect(target)
    counts = {}
    rules_path = voice_rules.path_for(target)
    created_rules = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        voice_schema.initialize(conn)
        for table, rows in bundle["tables"].items():
            inserted = 0
            cols = TABLES[table]
            names = ",".join(f'"{c}"' for c in cols)
            marks = ",".join("?" for _ in cols)
            for row in rows:
                cursor = conn.execute(
                    f'INSERT INTO "{table}" ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING',
                    [row[c] for c in cols],
                )
                inserted += cursor.rowcount
            counts[table] = {"inserted": inserted, "kept": len(rows) - inserted}
        if "rules" in bundle and not rules_path.exists():
            voice_rules.save(rules_path, bundle["rules"])
            created_rules = True
        conn.commit()
    except Exception:
        conn.rollback()
        if created_rules:
            rules_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    return {
        "backup": str(backup) if backup else None,
        "tables": counts,
        "rules": "created" if created_rules else "kept existing or not supplied",
    }


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def supabase_sql(bundle):
    """Generate reviewable transactional SQL; retain timestamps."""
    bundle = validate(bundle)
    lines = [
        "-- Synthetic demo data ONLY. Run supabase_setup.sql first.",
        "-- Existing keys win. No main safety-history tables are modified.",
        "-- Routing rules are PC JSON, not SQL: use db_transfer.py rules to extract them.",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    for table, rows in bundle["tables"].items():
        cols = TABLES[table]
        names = ", ".join(f'"{c}"' for c in cols)
        for row in rows:
            values = ", ".join(_literal(row[c]) for c in cols)
            lines.append(
                f'INSERT INTO public."{table}" ({names}) VALUES ({values}) ON CONFLICT DO NOTHING;'
            )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _write_new(path, text):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        out.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="합성 DB를 읽기 전용으로 내보내기")
    export.add_argument("--voice-db", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument(
        "--demo",
        action="store_true",
        required=True,
        help="입력이 실제 직원 자료가 아닌 합성 데이터임을 표시",
    )
    for name in ("check", "sql", "import", "rules"):
        sub = commands.add_parser(name)
        sub.add_argument("bundle", type=Path)
        if name in ("sql", "rules"):
            sub.add_argument("--output", required=True, type=Path)
        if name == "import":
            sub.add_argument("--db", required=True, type=Path)
    args = ap.parse_args()
    try:
        if args.command == "export":
            bundle = export_bundle(args.voice_db)
            _write_new(args.output, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        else:
            bundle = validate(json.loads(args.bundle.read_text(encoding="utf-8-sig")))
            if args.command == "sql":
                _write_new(args.output, supabase_sql(bundle))
            elif args.command == "rules":
                if "rules" not in bundle:
                    raise ValueError("이 묶음에는 음성 규칙이 없습니다")
                _write_new(
                    args.output, json.dumps(bundle["rules"], ensure_ascii=False, indent=2) + "\n"
                )
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
