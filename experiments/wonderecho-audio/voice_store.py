"""Voice operational data: settings, roster and additional phrases.

Fixed routing rules live in <DB stem>.rules.json (see voice_rules.py).
    python voice_store.py --seed
    python voice_store.py --dump
    python voice_store.py --rules          # show effective local rules
    python voice_store.py --add wake 메카봇 # edit the local rule file
    python voice_store.py --set follow_s 20
Remote reads/writes apply only to the three operational tables. Roster failures
remain fail-closed. Migrate old databases with migrate_voice_db.py first.
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
import voice_rules
from sqlite_admin import backup_database, has_rows, open_readonly

KST = timezone(timedelta(hours=9))
DEFAULT_DB = Path(__file__).with_name("voice_data.db")

# 비상정지 구문 — DB 오버레이가 제거·재매핑할 수 없는 최소 안전 집합.
PROTECTED_ACTIONS = ("비상정지", "긴급정지", "스톱")

SCHEMA = """
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

_KEYWORD_KINDS = voice_rules.KINDS
TABLES = ("settings", "roster", "phrases")

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
        tables = TABLES
        conn.execute("BEGIN IMMEDIATE")
        if not reset and has_rows(conn, tables):
            return False
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
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


def words(kind, default, db_path=None):
    return tuple(voice_rules.load(db_path or DEFAULT_DB)["keywords"].get(kind, default))


def command_endings(default, db_path=None):
    return sorted(
        voice_rules.load(db_path or DEFAULT_DB).get("command_endings", default),
        key=len,
        reverse=True,
    )


def action_commands(default, db_path=None):
    data = voice_rules.load(db_path or DEFAULT_DB).get("action_commands", default)
    base = {p: tuple(v) for p, v in data.items()}
    protected = voice_rules.defaults()["action_commands"]
    for phrase in PROTECTED_ACTIONS:
        base[phrase] = tuple(protected[phrase])
    return base


def scenario_triggers(default, db_path=None):
    return voice_rules.load(db_path or DEFAULT_DB).get("scenario_triggers", default)


def factory_rules(db_path=None):
    rows = voice_rules.load(db_path or DEFAULT_DB)["factory_rules"]
    return sorted((tuple(r) for r in rows), key=lambda r: r[4])


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


def validate_phrase(category, text):
    if not isinstance(category, str) or not re.fullmatch(r"[a-z0-9_]+", category):
        raise ValueError("문구 카테고리 형식 오류")
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or "\x00" in text:
        raise ValueError("문구는 비어 있지 않은 200자 이내 문자열이어야 합니다")


def edit_phrase(category, text, *, remove=False, db_path=None):
    """The admin UI and CLI use the same store; no separate JSON write path."""
    validate_phrase(category, text)
    if remote_enabled() and db_path is None:
        key = os.environ.get("SUPABASE_WRITE_KEY")
        if not key:
            raise ValueError("원격 문구 편집에는 관리용 SUPABASE_WRITE_KEY가 필요합니다")
        if remove:
            ok = suparest.delete_where(
                _REMOTE_URL,
                key,
                "phrases",
                {
                    "category": f"eq.{category}",
                    "phrase": f"eq.{text}",
                },
            )
        else:
            ok = suparest.upsert_rows(
                _REMOTE_URL,
                key,
                "phrases",
                {
                    "category": category,
                    "phrase": text,
                },
                ignore_duplicates=True,
            )
        if not ok:
            raise ValueError("원격 문구 저장 실패")
        for cache_key in list(_remote_cache):
            if cache_key[0] == "phrases":
                del _remote_cache[cache_key]
        return True
    try:
        conn = sqlite3.connect(db_path or DEFAULT_DB)
    except sqlite3.Error as exc:
        raise ValueError("문구 DB 열기 실패") from exc
    try:
        conn.executescript(SCHEMA)
        if remove:
            cursor = conn.execute(
                "DELETE FROM phrases WHERE category=? AND phrase=?", (category, text)
            )
        else:
            cursor = conn.execute("INSERT OR IGNORE INTO phrases VALUES (?,?)", (category, text))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise ValueError("문구 DB 저장 실패") from exc
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
        for t in TABLES:
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
        help=f"로컬 JSON 호출어 추가 ({'|'.join(_KEYWORD_KINDS)})",
    )
    ap.add_argument(
        "--del", dest="remove", nargs=2, metavar=("KIND", "WORD"), help="로컬 JSON 호출어 삭제"
    )
    ap.add_argument("--add-roster", metavar="NAME", help="직원 명단에 추가")
    ap.add_argument("--del-roster", metavar="NAME", help="직원 명단에서 삭제")
    ap.add_argument("--add-phrase", nargs=2, metavar=("CAT", "TEXT"), help="응답 문구 추가")
    ap.add_argument("--del-phrase", nargs=2, metavar=("CAT", "TEXT"), help="응답 문구 삭제")
    ap.add_argument(
        "--remote",
        action="store_true",
        help="위 작업을 로컬 DB가 아니라 Supabase에 적용 (SUPABASE_URL + WRITE_KEY 필요)",
    )
    ap.add_argument("--rules", action="store_true", help="유효한 로컬 규칙 JSON 출력")
    args = ap.parse_args()
    if args.remote and (args.add or args.remove or args.rules):
        ap.error("고정 규칙은 PC의 JSON 파일에서 관리합니다. --remote와 함께 쓸 수 없습니다")
    for pair in (args.add_phrase, args.del_phrase):
        if pair:
            validate_phrase(*pair)
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
        path = voice_rules.path_for(args.db)
        data = voice_rules.read(path) if path.exists() else voice_rules.defaults()
        for operation, pair in (("add", args.add), ("remove", args.remove)):
            if not pair:
                continue
            kind, word = pair
            if kind not in _KEYWORD_KINDS:
                ap.error(f"kind must be one of {_KEYWORD_KINDS}")
            values = data["keywords"][kind]
            if operation == "add" and word not in values:
                values.append(word)
            elif operation == "remove" and word in values:
                values.remove(word)
        voice_rules.save(path, data)
        print(f"[rules] {path}")
    if args.rules:
        print(json.dumps(voice_rules.load(args.db), ensure_ascii=False, indent=2))
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
