"""Virtual MES — read-only factory data API (auxiliary feature).

데이터 원본은 둘 중 하나다:
- 로컬 SQLite 파일 (기본, --seed로 데모 데이터 생성)
- Supabase 테이블 (MES_BACKEND=supabase + SUPABASE_URL + SUPABASE_ANON_KEY)

실제 MES가 생기면 이 파일의 데이터 소스만 실 API로 교체하면 되고,
음성 쪽(factorylink.py)은 URL만 바뀐다.

스키마는 제안서 데이터 계약을 따른다:
  production_status  — 라인별 목표/완료/잔량/상태
  shipment_schedule  — 납품·출하 일정
  work_schedule      — 작업지시와 우선순위
  inspection_log     — 검사 결과와 불량 수량
  equipment_check    — 설비 점검 기록

모든 응답은 값과 함께 source·updated_at·stale를 돌려준다. stale 판정은
서버가 한다 (updated_at 이 --stale-after 초보다 오래되면 true).

사용:
  python factory_mes.py --seed            # 빈 DB에 합성 데이터 생성 (--reset은 백업 후 재생성)
  python factory_mes.py --serve --port 8095
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import suparest
from sqlite_admin import backup_database, has_rows, open_readonly

KST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).with_name("mes_demo.db")
SOURCE = "demo-mes"
DEFAULT_STALE_AFTER = 6 * 3600  # 6시간 — 데모 데이터는 seed 시각 기준


class SourceError(Exception):
    """원격 데이터 원본(Supabase)이 응답하지 않을 때 — 503으로 변환된다."""


class Backend:
    """행 읽기 원본 — sqlite 파일 또는 Supabase PostgREST."""

    def __init__(self, db_path=None, url="", key=""):
        self.db_path = db_path
        self.url = (url or "").rstrip("/")
        self.key = key or ""

    @property
    def remote(self):
        return bool(self.url and self.key)

    @classmethod
    def from_env(cls, db_path=DEFAULT_DB, *, backend=None):
        """MES_BACKEND=supabase이면 SUPABASE_URL/ANON_KEY로 원격을 연다."""
        if (backend or os.environ.get("MES_BACKEND", "sqlite")) != "supabase":
            return cls(db_path=str(db_path))
        url = os.environ.get("MES_SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
        key = (
            os.environ.get("MES_SUPABASE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
            or os.environ.get("SUPABASE_KEY", "")
        )
        return cls(url=url, key=key)


SCHEMA = """
CREATE TABLE IF NOT EXISTS production_status (
    line_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    target_quantity INTEGER NOT NULL,
    completed_quantity INTEGER NOT NULL,
    state TEXT NOT NULL,               -- running | stopped | idle
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipment_schedule (
    shipment_id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    product TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    deadline TEXT NOT NULL,            -- ISO 날짜
    dock TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_schedule (
    task_id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL,          -- 1 = 가장 급함
    start_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending | in_progress | done
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id TEXT NOT NULL,
    lot TEXT NOT NULL,
    inspected INTEGER NOT NULL,
    defects INTEGER NOT NULL,
    result TEXT NOT NULL,               -- pass | fail | hold
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment_check (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment TEXT NOT NULL,
    line_id TEXT NOT NULL,
    check_item TEXT NOT NULL,
    result TEXT NOT NULL,               -- ok | warn | fail
    checked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(KST).isoformat(timespec="seconds")


def seed(db_path=DEFAULT_DB, *, reset=False):
    """빈 DB만 합성 자료로 초기화. reset은 백업 후 명시적으로 재생성한다."""
    now = _now()
    today = datetime.now(KST).date()
    if reset:
        backup_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        tables = (
            "production_status",
            "shipment_schedule",
            "work_schedule",
            "inspection_log",
            "equipment_check",
        )
        conn.execute("BEGIN IMMEDIATE")
        if not reset and has_rows(conn, tables):
            return False
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO production_status VALUES (?,?,?,?,?,?)",
            [
                ("A", "MD-100 구동모듈", 1200, 780, "running", now),
                ("B", "MD-200 센서모듈", 800, 800, "idle", now),
                ("C", "MD-100 구동모듈", 600, 210, "stopped", now),
            ],
        )
        # 출하 번호·LOT는 마감일/시드일에서 유도한다 — 다른 날 --seed 해도
        # ID와 날짜가 어긋나지 않게.
        ship_rows = []
        for i, (customer, product, qty, dplus, dock) in enumerate(
            [
                ("한국정밀", "MD-100 구동모듈", 400, 1, "2번 도크"),
                ("대성산업", "MD-200 센서모듈", 300, 3, "1번 도크"),
                ("한국정밀", "MD-100 구동모듈", 600, 5, "미정"),
            ],
            start=1,
        ):
            dl = today + timedelta(days=dplus)
            ship_rows.append(
                (f"SH-{dl:%m%d}-{i:02d}", customer, product, qty, dl.isoformat(), dock, now)
            )
        conn.executemany("INSERT INTO shipment_schedule VALUES (?,?,?,?,?,?,?)", ship_rows)
        conn.executemany(
            "INSERT INTO work_schedule VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    "WO-1001",
                    "A",
                    "MD-100 구동모듈 잔량 생산",
                    1,
                    now,
                    (today + timedelta(days=1)).isoformat(),
                    "in_progress",
                    now,
                ),
                (
                    "WO-1002",
                    "C",
                    "C라인 정지 원인 점검 후 재가동",
                    2,
                    now,
                    (today + timedelta(days=2)).isoformat(),
                    "pending",
                    now,
                ),
                (
                    "WO-1003",
                    "B",
                    "MD-200 후속 물량 준비",
                    3,
                    now,
                    (today + timedelta(days=4)).isoformat(),
                    "pending",
                    now,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO inspection_log(line_id,lot,inspected,defects,result,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            [
                ("A", f"LOT-A{today:%m%d}", 200, 3, "pass", now),
                ("B", f"LOT-B{today - timedelta(days=1):%m%d}", 300, 0, "pass", now),
                ("C", f"LOT-C{today:%m%d}", 80, 12, "hold", now),
            ],
        )
        conn.executemany(
            "INSERT INTO equipment_check"
            "(equipment,line_id,check_item,result,checked_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            [
                ("프레스-01", "A", "유압·안전센서", "ok", now, now),
                ("컨베이어-03", "C", "벨트 장력", "warn", now, now),
                ("로딩로봇-01", "B", "그리퍼 캘리브레이션", "ok", now, now),
            ],
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


_OPS = {"eq": "=", "neq": "!=", "lte": "<=", "gte": ">="}


def _fetch(src, table, filters=(), order=None, desc=False, limit=None):
    """공통 행 조회 — remote는 PostgREST 필터, sqlite는 같은 조건의 SQL.

    filters: [(col, op, val)] — op는 _OPS 키. PostgREST `col=op.val`과
    SQL `col OP ?` 양쪽으로 컴파일된다.
    """
    if src.remote:
        params = {c: f"{op}.{v}" for c, op, v in filters}
        if order:
            params["order"] = f"{order}.{'desc' if desc else 'asc'}"
        if limit:
            params["limit"] = str(int(limit))
        rows = suparest.get_rows(src.url, src.key, table, params)
        if rows is None:
            raise SourceError(table)
        return rows
    sql = f"SELECT * FROM {table}"
    args = [v for _, _, v in filters]
    if filters:
        sql += " WHERE " + " AND ".join(f"{c} {_OPS[o]} ?" for c, o, _ in filters)
    if order:
        sort_column = f"julianday({order})" if order in ("updated_at", "checked_at") else order
        sql += f" ORDER BY {sort_column} {'DESC' if desc else 'ASC'}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = open_readonly(src.db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _mark_stale(rows, stale_after):
    now = datetime.now(KST)
    for r in rows:
        r["source"] = SOURCE
        try:
            ts = datetime.fromisoformat(r["updated_at"])
            age = (now - ts).total_seconds()
            r["stale"] = not 0 <= age <= stale_after
        except (KeyError, TypeError, ValueError):
            r["stale"] = True
        r["fresh"] = not r["stale"]
    return rows


def _known_lines(src):
    """모든 테이블에 등장하는 라인 ID — '등록된 라인'의 정본."""
    out = set()
    for t in ("production_status", "work_schedule", "inspection_log", "equipment_check"):
        out.update(r["line_id"] for r in _fetch(src, t) if r.get("line_id"))
    return sorted(out)


def _latest_per_equipment(rows):
    """설비별 가장 최근 점검 행 — remote엔 GROUP BY가 없어서 코드에서 고른다."""

    def checked(row):
        try:
            ts = datetime.fromisoformat(row["checked_at"])
            return ts.timestamp() if ts.tzinfo is not None else float("-inf")
        except (KeyError, TypeError, ValueError):
            return float("-inf")

    latest = {}
    for r in rows:
        eq = r.get("equipment")
        if eq and (eq not in latest or checked(r) > checked(latest[eq])):
            latest[eq] = r
    return list(latest.values())


def query(src, endpoint, params, stale_after):
    """엔드포인트 → (rows, error). error가 있으면 rows 대신 메시지 dict."""
    line = (params.get("line") or [None])[0]
    if line:
        line = line.upper()
    # 라인 파라미터를 받는 엔드포인트는 공통으로 미등록 라인을 걸러낸다 —
    # 없는 라인과 '기록 0건'을 구분 못 하면 음성 답변이 오해를 만든다.
    line_eps = ("production", "schedule", "inspections", "equipment")
    if line and endpoint in line_eps:
        known = _known_lines(src)
        if line not in known:
            return {"error": "unknown_line", "valid_lines": known}
    if endpoint == "production":
        rows = _fetch(
            src,
            "production_status",
            [("line_id", "eq", line)] if line else (),
            order="line_id",
        )
        # 잔량은 코드에서 계산 — PostgREST에 계산열이 없어서 양쪽 경로 통일.
        for r in rows:
            try:
                r["remaining_quantity"] = int(r["target_quantity"]) - int(r["completed_quantity"])
            except (KeyError, TypeError, ValueError):
                r["remaining_quantity"] = None
    elif endpoint == "shipments":
        try:
            days = int((params.get("days") or [7])[0])
        except (TypeError, ValueError):
            return {"error": "bad_param", "detail": "days must be an integer"}
        limit = (datetime.now(KST).date() + timedelta(days=days)).isoformat()
        rows = _fetch(
            src,
            "shipment_schedule",
            [("deadline", "lte", limit)],
            order="deadline",
        )
    elif endpoint == "schedule":
        filters = [("status", "neq", "done")]
        if line:
            filters.append(("line_id", "eq", line))
        rows = _fetch(src, "work_schedule", filters, order="priority")
    elif endpoint == "inspections":
        rows = _fetch(
            src,
            "inspection_log",
            [("line_id", "eq", line)] if line else (),
            order="updated_at",
            desc=True,
            limit=10,
        )
    elif endpoint == "equipment":
        rows = _fetch(src, "equipment_check", [("line_id", "eq", line)] if line else ())
        rows = _latest_per_equipment(rows)
    else:
        return {"error": "unknown_endpoint"}
    return _mark_stale(rows, stale_after)


class Handler(BaseHTTPRequestHandler):
    src = Backend(db_path=DEFAULT_DB)
    stale_after = DEFAULT_STALE_AFTER

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self._json(
                {
                    "ok": True,
                    "source": SOURCE,
                    "backend": "supabase" if self.src.remote else "sqlite",
                }
            )
        ep = u.path.removeprefix("/api/")
        if ep not in ("production", "shipments", "schedule", "inspections", "equipment"):
            return self._json({"error": "not found"}, 404)
        try:
            res = query(self.src, ep, parse_qs(u.query), self.stale_after)
        except SourceError:
            return self._json({"error": "source unreachable"}, 503)
        except sqlite3.Error as e:
            return self._json({"error": f"db: {e}"}, 500)
        if isinstance(res, dict) and res.get("error"):
            code = {"unknown_endpoint": 404, "bad_param": 400}.get(res["error"], 200)
            return self._json(res, code)
        return self._json({"data": res})

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(port, src, stale_after):
    Handler.src = src
    Handler.stale_after = stale_after
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    kind = f"supabase {src.url}" if src.remote else f"sqlite {src.db_path}"
    print(f"[mes] {SOURCE} on :{port} backend={kind} stale_after={stale_after}s")
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seed", action="store_true", help="빈 로컬 DB에 합성 데이터 생성")
    ap.add_argument("--reset", action="store_true", help="--seed와 함께: 백업 후 데모 재생성")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument(
        "--backend",
        choices=("sqlite", "supabase"),
        default=os.environ.get("MES_BACKEND", "sqlite"),
        help="supabase이면 SUPABASE_URL + SUPABASE_ANON_KEY 환경변수 필요",
    )
    ap.add_argument(
        "--stale-after",
        type=int,
        default=DEFAULT_STALE_AFTER,
        help="이 초보다 오래된 updated_at은 stale:true",
    )
    args = ap.parse_args()
    if args.reset and not args.seed:
        ap.error("--reset은 --seed와 함께만 사용합니다")
    if args.seed:
        changed = seed(args.db, reset=args.reset)
        print(f"[mes] {'seeded' if changed else 'kept existing data'} {args.db}")
    if args.serve:
        if args.backend == "supabase":
            src = Backend.from_env(backend=args.backend)
            if not src.remote:
                ap.error("supabase 백엔드에는 SUPABASE_URL과 SUPABASE_ANON_KEY가 필요합니다")
        else:
            if not args.db.exists():
                seed(args.db)
            src = Backend(db_path=str(args.db))
        serve(args.port, src, args.stale_after)


if __name__ == "__main__":
    main()
