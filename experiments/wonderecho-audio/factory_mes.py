"""Virtual MES — SQLite-backed read-only factory data API (auxiliary feature).

시연용 가상 MES다. 실제 MES가 생기면 이 파일의 데이터 소스만 실 API로
교체하면 되고, 음성 쪽(factorylink.py)은 URL만 바뀐다.

스키마는 제안서 데이터 계약을 따른다:
  production_status  — 라인별 목표/완료/잔량/상태
  shipment_schedule  — 납품·출하 일정
  work_schedule      — 작업지시와 우선순위
  inspection_log     — 검사 결과와 불량 수량
  equipment_check    — 설비 점검 기록

모든 응답은 값과 함께 source·updated_at·stale를 돌려준다. stale 판정은
서버가 한다 (updated_at 이 --stale-after 초보다 오래되면 true).

사용:
  python factory_mes.py --seed            # 데모 DB 생성/재생성
  python factory_mes.py --serve --port 8095
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).with_name("mes_demo.db")
SOURCE = "demo-mes"
DEFAULT_STALE_AFTER = 6 * 3600  # 6시간 — 데모 데이터는 seed 시각 기준

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


def seed(db_path=DEFAULT_DB):
    """데모 데이터를 심는다 — 기존 테이블은 비우고 다시 채운다."""
    now = _now()
    today = datetime.now(KST).date()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for t in (
            "production_status",
            "shipment_schedule",
            "work_schedule",
            "inspection_log",
            "equipment_check",
        ):
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO production_status VALUES (?,?,?,?,?,?)",
            [
                ("A", "MD-100 구동모듈", 1200, 780, "running", now),
                ("B", "MD-200 센서모듈", 800, 800, "idle", now),
                ("C", "MD-100 구동모듈", 600, 210, "stopped", now),
            ],
        )
        conn.executemany(
            "INSERT INTO shipment_schedule VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "SH-0917-01",
                    "한국정밀",
                    "MD-100 구동모듈",
                    400,
                    (today + timedelta(days=1)).isoformat(),
                    "2번 도크",
                    now,
                ),
                (
                    "SH-0918-01",
                    "대성산업",
                    "MD-200 센서모듈",
                    300,
                    (today + timedelta(days=3)).isoformat(),
                    "1번 도크",
                    now,
                ),
                (
                    "SH-0920-01",
                    "한국정밀",
                    "MD-100 구동모듈",
                    600,
                    (today + timedelta(days=5)).isoformat(),
                    "미정",
                    now,
                ),
            ],
        )
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
                ("A", "LOT-A0917", 200, 3, "pass", now),
                ("B", "LOT-B0916", 300, 0, "pass", now),
                ("C", "LOT-C0917", 80, 12, "hold", now),
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
    finally:
        conn.close()


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


def _mark_stale(rows, stale_after):
    now = datetime.now(KST)
    for r in rows:
        r["source"] = SOURCE
        try:
            ts = datetime.fromisoformat(r["updated_at"])
            r["stale"] = (now - ts).total_seconds() > stale_after
        except (KeyError, ValueError):
            r["stale"] = True
    return rows


def query(db_path, endpoint, params, stale_after):
    """엔드포인트 → (rows, error). error가 있으면 rows 대신 메시지 dict."""
    conn = sqlite3.connect(db_path)
    try:
        if endpoint == "production":
            line = (params.get("line") or [None])[0]
            if line:
                rows = _rows(
                    conn,
                    "SELECT *, target_quantity - completed_quantity AS"
                    " remaining_quantity FROM production_status WHERE line_id=?",
                    (line.upper(),),
                )
                if not rows:
                    valid = [r["line_id"] for r in _rows(
                        conn, "SELECT line_id FROM production_status ORDER BY line_id"
                    )]
                    return {"error": "unknown_line", "valid_lines": valid}
            else:
                rows = _rows(
                    conn,
                    "SELECT *, target_quantity - completed_quantity AS"
                    " remaining_quantity FROM production_status ORDER BY line_id",
                )
        elif endpoint == "shipments":
            days = int((params.get("days") or [7])[0])
            limit = (datetime.now(KST).date() + timedelta(days=days)).isoformat()
            rows = _rows(
                conn,
                "SELECT * FROM shipment_schedule WHERE deadline<=?"
                " ORDER BY deadline",
                (limit,),
            )
        elif endpoint == "schedule":
            line = (params.get("line") or [None])[0]
            sql = (
                "SELECT * FROM work_schedule WHERE status!='done'"
            )
            args = ()
            if line:
                sql += " AND line_id=?"
                args = (line.upper(),)
            rows = _rows(conn, sql + " ORDER BY priority", args)
        elif endpoint == "inspections":
            line = (params.get("line") or [None])[0]
            sql = "SELECT * FROM inspection_log"
            args = ()
            if line:
                sql += " WHERE line_id=?"
                args = (line.upper(),)
            rows = _rows(conn, sql + " ORDER BY id DESC LIMIT 10", args)
        elif endpoint == "equipment":
            line = (params.get("line") or [None])[0]
            sql = (
                "SELECT * FROM equipment_check e WHERE id IN"
                " (SELECT MAX(id) FROM equipment_check GROUP BY equipment)"
            )
            args = ()
            if line:
                sql += " AND e.line_id=?"
                args = (line.upper(),)
            rows = _rows(conn, sql, args)
        else:
            return {"error": "unknown_endpoint"}
        return _mark_stale(rows, stale_after)
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    stale_after = DEFAULT_STALE_AFTER

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self._json({"ok": True, "source": SOURCE})
        ep = u.path.removeprefix("/api/")
        if ep not in ("production", "shipments", "schedule", "inspections", "equipment"):
            return self._json({"error": "not found"}, 404)
        try:
            res = query(self.db_path, ep, parse_qs(u.query), self.stale_after)
        except sqlite3.Error as e:
            return self._json({"error": f"db: {e}"}, 500)
        if isinstance(res, dict) and res.get("error") == "unknown_endpoint":
            return self._json(res, 404)
        return self._json({"data": res} if isinstance(res, list) else res)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(port, db_path, stale_after):
    Handler.db_path = db_path
    Handler.stale_after = stale_after
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[mes] {SOURCE} on :{port} db={db_path} stale_after={stale_after}s")
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seed", action="store_true", help="데모 데이터 재생성")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument(
        "--stale-after",
        type=int,
        default=DEFAULT_STALE_AFTER,
        help="이 초보다 오래된 updated_at은 stale:true",
    )
    args = ap.parse_args()
    if args.seed:
        seed(args.db)
        print(f"[mes] seeded {args.db}")
    if args.serve:
        if not args.db.exists():
            seed(args.db)
        serve(args.port, str(args.db), args.stale_after)


if __name__ == "__main__":
    main()
