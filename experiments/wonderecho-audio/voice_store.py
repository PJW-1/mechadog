"""Voice-module config store — 암구호·트리거·설정의 DB 오버레이.

`voice_data.db`(SQLite)가 있으면 테이블 내용이 코드 기본값보다 우선한다.
없으면 코드 기본값 그대로 — **DB는 필수가 아니라 오버레이**다.

    python voice_store.py --seed          # 코드 기본값으로 빈 로컬 DB 초기화
    python voice_store.py --dump          # 현재 테이블 내용 출력
    python voice_store.py --set KEY VALUE # settings 값 변경
    python voice_store.py --add KIND WORD # keywords 행 추가 (wake/sleep/...)
    python voice_store.py --del KIND WORD # keywords 행 삭제
    python voice_store.py --remote ...    # 같은 명령을 Supabase에 적용 (SUPABASE_WRITE_KEY 필요)
    python voice_store.py --seed --remote # 코드 기본값을 Supabase로 밀어 넣기

원격 백엔드(Supabase):
- SUPABASE_URL + SUPABASE_ANON_KEY가 있으면 읽기는 PostgREST가 우선이고
  TTL 캐시(기본 60s, VOICE_STORE_TTL로 변경)로 매 턴 왕복을 막는다.
- 읽기 사슬: Supabase → 로컬 voice_data.db → 코드 기본값. 일반 설정은 앞 단계가
  비거나 실패하면 다음으로 내려간다. roster는 빈 결과·오류 시 승인 대상 0명이다.
- 쓰기(--remote)는 SUPABASE_WRITE_KEY(service role)가 필요하다.
  음성 PC에는 anon 키만 두고 쓰기 키는 관리 도구에만 둘 것.

설계 원칙:
- 기본값의 정본은 각 소비자 모듈의 상수다. --seed는 그 값을 DB로 옮길 뿐이다.
- DB 파일이 없거나 해당 항목이 비어 있으면 코드 기본값이 쓰인다 — CI·새 클론·
  테스트는 DB 없이도 완전히 동작해야 한다.
- estop 계열(PROTECTED_ACTIONS)은 DB가 지우거나 다른 명령으로 바꿔도 항상
  코드 기본값이 합쳐진다 — 운영 실수로 비상정지가 죽지 않는다.
- 이 DB는 "무슨 말이 트리거인가"만 바꾼다. 명령 실행 자체는 robotlink의
  화이트리스트와 로봇 런타임 게이트가 계속 담당한다.
- MES 데이터(mes_demo.db / Supabase MES 테이블)는 '외부 시스템'이라 별도다.
  지식 문서(knowledge/*.txt)는 RAG 입력이라 파일을 유지한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import suparest
from sqlite_admin import backup_database, has_rows, open_readonly

KST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).with_name("voice_data.db")

# 비상정지 구문 — DB 오버레이가 제거·재매핑할 수 없는 최소 안전 집합.
PROTECTED_ACTIONS = ("비상정지", "긴급정지", "스톱")

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    kind TEXT NOT NULL,            -- wake|sleep|resume|emergency|status|machine
    word TEXT NOT NULL,
    PRIMARY KEY (kind, word)
);
CREATE TABLE IF NOT EXISTS command_endings (
    ending TEXT PRIMARY KEY        -- "해줘/로전환해줘" 등 벗겨낼 명령 어미
);
CREATE TABLE IF NOT EXISTS action_commands (
    phrase TEXT PRIMARY KEY,       -- 정규화된 발화
    action TEXT NOT NULL,          -- estop|manual_on|manual_off|patrol_start|patrol_stop
    ack TEXT NOT NULL              -- 실행 후 읽는 확인 멘트
);
CREATE TABLE IF NOT EXISTS scenario_triggers (
    phrase TEXT PRIMARY KEY,
    scenario TEXT NOT NULL         -- scenarios.SCENARIOS 키
);
CREATE TABLE IF NOT EXISTS factory_rules (
    keyword TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,        -- production|shipments|schedule|inspections|equipment
    needs_line INTEGER NOT NULL DEFAULT 0,  -- 라인 표기가 있을 때만 적용
    attach_line INTEGER NOT NULL DEFAULT 0, -- params에 line을 담을지
    priority INTEGER NOT NULL DEFAULT 100   -- 작을수록 먼저 평가
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (
    name TEXT PRIMARY KEY,           -- 신원 확인 명단 (scenarios.sc_guard)
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS phrases (
    category TEXT NOT NULL,          -- phrases.PHRASES 카테고리 키
    phrase TEXT NOT NULL,
    PRIMARY KEY (category, phrase)
);
"""

_KEYWORD_KINDS = ("wake", "sleep", "resume", "emergency", "status", "machine")

# settings 키가 이 패턴에 걸리면 CLI 출력(--set 에코, --dump)에서 값을 가린다.
# 암구호·방문자 코드 같은 값을 콘솔·로그에 평문으로 남기지 않기 위해서다.
_SENSITIVE_KEY_RE = re.compile(r"pass|secret|token|code|key", re.I)


def _code_defaults():
    """각 모듈 상수에서 기본값을 모은다 — 순환 임포트 방지로 지연 임포트."""
    import factorylink
    import robotlink
    import scenarios
    import voice_pipeline as vp

    return {
        "keywords": {
            "wake": list(vp.WAKE_PREFIXES),
            "sleep": list(vp.SLEEP_WORDS),
            "resume": list(vp.RESUME_WORDS),
            "emergency": list(vp.EMERGENCY_WORDS),
            "status": list(robotlink._STATUS_WORDS),
            "machine": list(vp._MACHINE_WORDS),
        },
        "command_endings": list(robotlink._COMMAND_ENDINGS),
        "action_commands": dict(robotlink.ACTIONS),
        "scenario_triggers": dict(scenarios.TRIGGERS),
        "factory_rules": list(factorylink.DEFAULT_RULES),
        "roster": scenarios._file_roster(),
        "settings": {
            "follow_s": str(vp.FOLLOW_S),
            "follow_min_chars": "3",
            "stt_prompt": vp.STT_PROMPT,
            "machine_notice": vp._MACHINE_NOTICE,
            "robot_api_base": robotlink.DEFAULT_BASE,
            "mes_api_base": factorylink.DEFAULT_BASE,
        },
    }


def seed(db_path=DEFAULT_DB, *, reset=False):
    """빈 DB만 초기화한다. 명시적인 reset은 먼저 SQLite 백업을 남긴다."""
    d = _code_defaults()
    now = datetime.now(KST).isoformat(timespec="seconds")
    if reset:
        backup_database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        tables = (
            "keywords",
            "command_endings",
            "action_commands",
            "scenario_triggers",
            "factory_rules",
            "settings",
            "roster",
            "phrases",
        )
        conn.execute("BEGIN IMMEDIATE")
        if not reset and has_rows(conn, tables):
            return False
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.executemany(
            "INSERT INTO keywords VALUES (?,?)",
            [(kind, w) for kind, words in d["keywords"].items() for w in words],
        )
        conn.executemany(
            "INSERT INTO command_endings VALUES (?)", [(e,) for e in d["command_endings"]]
        )
        conn.executemany(
            "INSERT INTO action_commands VALUES (?,?,?)",
            [(p, act, ack) for p, (act, ack) in d["action_commands"].items()],
        )
        conn.executemany(
            "INSERT INTO scenario_triggers VALUES (?,?)",
            list(d["scenario_triggers"].items()),
        )
        conn.executemany(
            "INSERT INTO factory_rules VALUES (?,?,?,?,?)",
            [tuple(r) for r in d["factory_rules"]],
        )
        conn.executemany(
            "INSERT INTO settings VALUES (?,?,?)",
            [(k, v, now) for k, v in d["settings"].items()],
        )
        conn.executemany(
            "INSERT INTO roster VALUES (?,?)",
            [(name, "직원명단.txt") for name in d["roster"]],
        )
        # phrases 테이블은 '추가 문구' 전용 — 시드는 비워 두고 기본 문구는 코드에 둔다.
        conn.commit()
        return True
    finally:
        conn.close()


def _connect(db_path):
    p = Path(db_path or DEFAULT_DB)
    try:
        return open_readonly(p) if p.is_file() else None
    except (OSError, sqlite3.Error):
        return None


# ── Supabase 원격 백엔드 ────────────────────────────────────────────────
# 읽기 사슬: Supabase → 로컬 voice_data.db → 코드 기본값.
# 원격이 "실패(None)"하거나 "비어 있으면" 다음 단계로 내려간다 — 테이블을
# 비운 경우 일반 설정은 기본값으로 복원한다. roster는 별도로 fail-closed 처리한다.
_REMOTE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_REMOTE_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY", "")
_REMOTE_TTL = float(os.environ.get("VOICE_STORE_TTL", "60"))
_remote_cache = {}  # (table, qs) -> (monotonic_ts, rows)


def remote_enabled():
    return bool(_REMOTE_URL and _REMOTE_KEY)


def _remote_rows(table, params=None, *, allow_stale=True):
    """Supabase 행 목록 → list | None(실패·미설정). TTL 캐시 + 장애 시 오래된 캐시."""
    if not remote_enabled():
        return None
    ck = (table, json.dumps(params or {}, sort_keys=True, ensure_ascii=False))
    now = time.monotonic()
    hit = _remote_cache.get(ck)
    if hit and now - hit[0] < _REMOTE_TTL:
        return hit[1]
    rows = suparest.get_rows(_REMOTE_URL, _REMOTE_KEY, table, params)
    if rows is not None:
        _remote_cache[ck] = (now, rows)
        return rows
    return hit[1] if hit and allow_stale else None


def _col_or_default(sql, params, default, db_path):
    """1열 조회 → tuple. DB 없음·빈 결과·오류는 전부 코드 기본값."""
    conn = _connect(db_path)
    if conn is None:
        return tuple(default)
    try:
        rows = [r[0] for r in conn.execute(sql, params)]
        return tuple(rows) if rows else tuple(default)
    except sqlite3.Error:
        return tuple(default)
    finally:
        conn.close()


def words(kind, default, db_path=None):
    """keywords 테이블의 kind별 단어 목록 (비어 있으면 기본값)."""
    rows = _remote_rows("keywords", {"kind": f"eq.{kind}", "select": "word", "order": "word"})
    if rows:
        return tuple(r["word"] for r in rows)
    return _col_or_default(
        "SELECT word FROM keywords WHERE kind=? ORDER BY rowid",
        (kind,),
        default,
        db_path,
    )


def command_endings(default, db_path=None):
    """명령 어미 목록 — 긴 것부터 시도하는 기존 규칙을 그대로 둔다."""
    rows = _remote_rows("command_endings", {"select": "ending"})
    if rows:
        return sorted((r["ending"] for r in rows), key=len, reverse=True)
    rows = _col_or_default("SELECT ending FROM command_endings", (), default, db_path)
    return sorted(rows, key=len, reverse=True)


def action_commands(default, db_path=None):
    """phrase → (action, ack). DB가 있으면 DB가 정본 + 보호 구문 강제."""
    base = dict(default)
    rows = _remote_rows("action_commands", {"select": "phrase,action,ack"})
    if rows:
        base = {r["phrase"]: (r["action"], r["ack"]) for r in rows}
    else:
        conn = _connect(db_path)
        if conn is not None:
            try:
                rows = conn.execute("SELECT phrase, action, ack FROM action_commands").fetchall()
                if rows:
                    base = {p: (a, ack) for p, a, ack in rows}
            except sqlite3.Error:
                pass
            finally:
                conn.close()
    for p in PROTECTED_ACTIONS:
        if p in default:
            base[p] = default[p]
    return base


def scenario_triggers(default, db_path=None):
    """phrase → scenario 이름."""
    rows = _remote_rows("scenario_triggers", {"select": "phrase,scenario"})
    if rows:
        return {r["phrase"]: r["scenario"] for r in rows}
    conn = _connect(db_path)
    if conn is None:
        return dict(default)
    try:
        rows = conn.execute(
            "SELECT phrase, scenario FROM scenario_triggers ORDER BY rowid"
        ).fetchall()
        return dict(rows) if rows else dict(default)
    except sqlite3.Error:
        return dict(default)
    finally:
        conn.close()


_RULE_COLS = ("keyword", "endpoint", "needs_line", "attach_line", "priority")


def factory_rules(db_path=None):
    """(keyword, endpoint, needs_line, attach_line, priority) 우선순위순."""
    import factorylink  # 지연 임포트 — 기본값의 정본은 저쪽 모듈

    rows = _remote_rows("factory_rules", {"select": ",".join(_RULE_COLS), "order": "priority"})
    if rows:
        return [tuple(r[c] for c in _RULE_COLS) for r in rows]
    conn = _connect(db_path)
    if conn is not None:
        try:
            rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT keyword, endpoint, needs_line, attach_line, priority"
                    " FROM factory_rules ORDER BY priority, rowid"
                )
            ]
            if rows:
                return rows
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return sorted((tuple(r) for r in factorylink.DEFAULT_RULES), key=lambda r: r[4])


def setting(key, default, cast=str, db_path=None):
    """settings 키 하나 — 키가 있으면(빈 문자열도) 그 값, 없으면 기본값."""
    rows = _remote_rows("settings", {"key": f"eq.{key}", "select": "value", "limit": "1"})
    if rows:
        try:
            return cast(rows[0]["value"])
        except (TypeError, ValueError):
            return default
    conn = _connect(db_path)
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row is not None:
                try:
                    return cast(row[0])
                except (TypeError, ValueError):
                    return default
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return default


def roster(default, db_path=None):
    """설정된 명단이 비거나 실패하면 승인 대상 0명. DB 미사용 때만 파일 명단."""
    if remote_enabled():
        rows = _remote_rows("roster", {"select": "name", "order": "name"}, allow_stale=False)
        if rows is None or any(
            not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"].strip()
            for r in rows
        ):
            return ()
        return tuple(r["name"] for r in rows)
    p = Path(db_path or DEFAULT_DB)
    if not p.exists():
        return tuple(default)
    conn = _connect(p)
    if conn is None:
        return ()
    try:
        return tuple(r[0] for r in conn.execute("SELECT name FROM roster ORDER BY rowid"))
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def all_phrases(db_path=None):
    """DB 추가 문구 전체 [(category, phrase), ...] — 기본 문구는 코드에 둔다."""
    rows = _remote_rows("phrases", {"select": "category,phrase"})
    if rows is not None:
        return [(r["category"], r["phrase"]) for r in rows]
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        return [
            tuple(r)
            for r in conn.execute("SELECT category, phrase FROM phrases ORDER BY rowid").fetchall()
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _masked(key, value):
    return "***" if _SENSITIVE_KEY_RE.search(key) else value


def dump(db_path=DEFAULT_DB):
    conn = open_readonly(db_path)
    try:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        out = {}
        for t in tables:
            quoted = t.replace('"', '""')
            cols = [d[0] for d in conn.execute(f'SELECT * FROM "{quoted}"').description]
            rows = [
                dict(zip(cols, r, strict=False)) for r in conn.execute(f'SELECT * FROM "{quoted}"')
            ]
            if t == "settings":
                for r in rows:
                    r["value"] = _masked(r["key"], r["value"])
            out[t] = rows
        return out
    finally:
        conn.close()


def _remote_main(args, ap):
    """--remote 경로: 같은 작업을 Supabase에 적용한다 (쓰기 키 필요)."""
    wkey = os.environ.get("SUPABASE_WRITE_KEY")
    if args.dump and not any(
        (
            args.seed,
            args.set,
            args.add,
            args.remove,
            args.add_roster,
            args.del_roster,
            args.add_phrase,
            args.del_phrase,
        )
    ):
        wkey = wkey or _REMOTE_KEY
    if not (_REMOTE_URL and wkey):
        ap.error("원격 쓰기에는 SUPABASE_URL과 SUPABASE_WRITE_KEY가 필요합니다")

    def _ok(ok, what):
        print(f"[remote] {what}: {'ok' if ok else 'FAILED'}")
        if not ok:
            raise SystemExit(1)
        return ok

    if args.seed:
        d = _code_defaults()
        now = datetime.now(KST).isoformat(timespec="seconds")
        tables = {
            "keywords": [{"kind": k, "word": w} for k, ws in d["keywords"].items() for w in ws],
            "command_endings": [{"ending": e} for e in d["command_endings"]],
            "action_commands": [
                {"phrase": p, "action": a, "ack": ack}
                for p, (a, ack) in d["action_commands"].items()
            ],
            "scenario_triggers": [
                {"phrase": p, "scenario": s} for p, s in d["scenario_triggers"].items()
            ],
            "factory_rules": [
                dict(
                    zip(
                        ("keyword", "endpoint", "needs_line", "attach_line", "priority"),
                        (r[0], r[1], bool(r[2]), bool(r[3]), r[4]),
                        strict=True,
                    )
                )
                for r in d["factory_rules"]
            ],
            "settings": [
                {"key": k, "value": v, "updated_at": now} for k, v in d["settings"].items()
            ],
            "roster": [{"name": n, "note": "직원명단.txt"} for n in d["roster"]],
            # phrases는 '추가 문구' 전용 — 시드는 비워 둔다 (기본 문구는 코드 정본)
        }
        for t, rows in tables.items():
            _ok(
                suparest.upsert_rows(_REMOTE_URL, wkey, t, rows, ignore_duplicates=True),
                f"seed {t} ({len(rows)}건, 기존 키 보존)",
            )
    if args.set:
        _ok(
            suparest.upsert_rows(
                _REMOTE_URL,
                wkey,
                "settings",
                {
                    "key": args.set[0],
                    "value": args.set[1],
                    "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
                },
            ),
            f"set {args.set[0]} = {_masked(args.set[0], args.set[1])}",
        )
    if args.add:
        kind, word = args.add
        if kind not in _KEYWORD_KINDS:
            ap.error(f"kind must be one of {_KEYWORD_KINDS}")
        _ok(
            suparest.upsert_rows(_REMOTE_URL, wkey, "keywords", {"kind": kind, "word": word}),
            f"+{kind}: {word}",
        )
    if args.remove:
        kind, word = args.remove
        _ok(
            suparest.delete_where(
                _REMOTE_URL, wkey, "keywords", {"kind": f"eq.{kind}", "word": f"eq.{word}"}
            ),
            f"-{kind}: {word}",
        )
    if args.add_roster:
        _ok(
            suparest.upsert_rows(
                _REMOTE_URL, wkey, "roster", {"name": args.add_roster, "note": "cli"}
            ),
            f"+roster: {args.add_roster}",
        )
    if args.del_roster:
        _ok(
            suparest.delete_where(_REMOTE_URL, wkey, "roster", {"name": f"eq.{args.del_roster}"}),
            f"-roster: {args.del_roster}",
        )
    if args.add_phrase:
        _ok(
            suparest.upsert_rows(
                _REMOTE_URL,
                wkey,
                "phrases",
                {"category": args.add_phrase[0], "phrase": args.add_phrase[1]},
            ),
            f"+phrase[{args.add_phrase[0]}]",
        )
    if args.del_phrase:
        _ok(
            suparest.delete_where(
                _REMOTE_URL,
                wkey,
                "phrases",
                {"category": f"eq.{args.del_phrase[0]}", "phrase": f"eq.{args.del_phrase[1]}"},
            ),
            f"-phrase[{args.del_phrase[0]}]",
        )
    if args.dump:
        rkey = _REMOTE_KEY or wkey
        out = {}
        for t in (
            "keywords",
            "command_endings",
            "action_commands",
            "scenario_triggers",
            "factory_rules",
            "settings",
            "roster",
            "phrases",
        ):
            rows = suparest.get_rows(_REMOTE_URL, rkey, t)
            if t == "settings" and rows:
                for r in rows:
                    r["value"] = _masked(r.get("key", ""), r.get("value", ""))
            out[t] = rows if rows is not None else f"<error: {t} 조회 실패>"
        print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seed", action="store_true", help="빈 DB를 코드 기본값으로 초기화")
    ap.add_argument("--reset", action="store_true", help="--seed와 함께: 백업 후 로컬 DB 초기화")
    ap.add_argument("--dump", action="store_true", help="테이블 전체 JSON 출력")
    ap.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), help="settings 변경")
    ap.add_argument(
        "--add",
        nargs=2,
        metavar=("KIND", "WORD"),
        help=f"keywords 추가 ({'|'.join(_KEYWORD_KINDS)})",
    )
    ap.add_argument("--del", dest="remove", nargs=2, metavar=("KIND", "WORD"), help="keywords 삭제")
    ap.add_argument("--add-roster", metavar="NAME", help="직원 명단에 추가")
    ap.add_argument("--del-roster", metavar="NAME", help="직원 명단에서 삭제")
    ap.add_argument("--add-phrase", nargs=2, metavar=("CAT", "TEXT"), help="응답 문구 추가")
    ap.add_argument("--del-phrase", nargs=2, metavar=("CAT", "TEXT"), help="응답 문구 삭제")
    ap.add_argument(
        "--remote",
        action="store_true",
        help="위 작업을 로컬 DB가 아니라 Supabase에 적용 (SUPABASE_URL + WRITE_KEY 필요)",
    )
    args = ap.parse_args()
    if args.reset and (not args.seed or args.remote):
        ap.error("--reset은 로컬 --seed와 함께만 사용합니다")
    if args.remote:
        _remote_main(args, ap)
        return
    if args.seed:
        changed = seed(args.db, reset=args.reset)
        print(f"[store] {'seeded' if changed else 'kept existing data'} {args.db}")
    if args.set:
        conn = sqlite3.connect(args.db)
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO settings VALUES (?,?,?) ON CONFLICT(key)"
                " DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (
                    args.set[0],
                    args.set[1],
                    datetime.now(KST).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        print(f"[store] {args.set[0]} = {_masked(args.set[0], args.set[1])}")
    if args.add or args.remove:
        conn = sqlite3.connect(args.db)
        try:
            conn.executescript(SCHEMA)
            if args.add:
                kind, word = args.add
                if kind not in _KEYWORD_KINDS:
                    ap.error(f"kind must be one of {_KEYWORD_KINDS}")
                conn.execute("INSERT OR IGNORE INTO keywords VALUES (?,?)", (kind, word))
                print(f"[store] +{kind}: {word}")
            if args.remove:
                kind, word = args.remove
                n = conn.execute(
                    "DELETE FROM keywords WHERE kind=? AND word=?", (kind, word)
                ).rowcount
                print(f"[store] -{kind}: {word} ({n}건)")
            conn.commit()
        finally:
            conn.close()
    if args.add_roster or args.del_roster or args.add_phrase or args.del_phrase:
        conn = sqlite3.connect(args.db)
        try:
            conn.executescript(SCHEMA)
            if args.add_roster:
                conn.execute(
                    "INSERT OR IGNORE INTO roster VALUES (?,?)",
                    (args.add_roster, "cli"),
                )
                print(f"[store] +roster: {args.add_roster}")
            if args.del_roster:
                n = conn.execute("DELETE FROM roster WHERE name=?", (args.del_roster,)).rowcount
                print(f"[store] -roster: {args.del_roster} ({n}건)")
            if args.add_phrase:
                conn.execute("INSERT OR IGNORE INTO phrases VALUES (?,?)", tuple(args.add_phrase))
                print(f"[store] +phrase[{args.add_phrase[0]}]: {args.add_phrase[1]}")
            if args.del_phrase:
                n = conn.execute(
                    "DELETE FROM phrases WHERE category=? AND phrase=?",
                    tuple(args.del_phrase),
                ).rowcount
                print(f"[store] -phrase[{args.del_phrase[0]}] ({n}건)")
            conn.commit()
        finally:
            conn.close()
    if args.dump:
        print(json.dumps(dump(args.db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
