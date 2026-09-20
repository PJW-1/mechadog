"""Canonical SQLite voice schema. Legacy tables are migration input only."""

VERSION = 2
COLUMNS = {
    "settings": ("key", "value", "updated_at"),
    "roster": ("name", "note"),
    "phrases": ("category", "phrase"),
}
PRIMARY_KEYS = {"settings": ("key",), "roster": ("name",), "phrases": ("category", "phrase")}
LEGACY = {
    "keywords": ("kind", "word"),
    "command_endings": ("ending",),
    "action_commands": ("phrase", "action", "ack"),
    "scenario_triggers": ("phrase", "scenario"),
    "factory_rules": ("keyword", "endpoint", "needs_line", "attach_line", "priority"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (
    name TEXT PRIMARY KEY,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS phrases (
    category TEXT NOT NULL,
    phrase TEXT NOT NULL,
    PRIMARY KEY (category, phrase)
);
"""


def validate(conn, *, allow_empty=False, allow_legacy=False, extra_tables=()):
    """Reject mixed, partial, foreign or future schemas without modifying them.

    Version 0 with the exact three-table layout is the first optimized release;
    it can be read, and explicit migration stamps it with VERSION after backup.
    extra_tables is only for the offline voice+MES preview/import container.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, VERSION):
        raise ValueError(f"지원하지 않는 음성 DB 버전 {version}; 현재 버전은 {VERSION}입니다")
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("sqlite_")
    }
    if names & LEGACY.keys() and not allow_legacy:
        raise ValueError("구형 음성 DB: python migrate_voice_db.py --db <DB경로>로 먼저 이전하세요")
    allowed = set(COLUMNS) | set(extra_tables) | (set(LEGACY) if allow_legacy else set())
    if names - allowed:
        raise ValueError("음성 DB에 다른 영역의 테이블이 있습니다. DB 경로를 확인하세요")
    if not (names & COLUMNS.keys()) and allow_empty and names <= set(extra_tables):
        return version
    if not COLUMNS.keys() <= names:
        raise ValueError("음성 DB의 settings/roster/phrases 세 테이블이 모두 필요합니다")
    for table, columns in COLUMNS.items():
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        pk = tuple(r[1] for r in sorted(info, key=lambda r: r[5]) if r[5])
        if (
            tuple(r[1] for r in info) != columns
            or any(r[2].upper() != "TEXT" for r in info)
            or pk != PRIMARY_KEYS[table]
        ):
            raise ValueError(f"{table}의 컬럼/타입/기본키가 음성 DB 정본과 다릅니다")
    return version


def initialize(conn, *, extra_tables=()):
    """Create/stamp canonical tables inside the caller's transaction; never commit."""
    validate(conn, allow_empty=True, extra_tables=extra_tables)
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version={VERSION}")
