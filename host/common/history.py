"""사건 이력 DB — 관제 사건과 순찰 실행을 나중에 찾아볼 수 있게 남긴다 (WBS 4.6.6 · ADR-46).

이 DB 는 **색인**이다. 사진과 판단 근거의 원본은 블랙박스 폴더에 있고, DB 가 깨지면
`tools/ops/history_import.py` 로 블랙박스에서 다시 만든다.

⚠️ **쓰기 실패는 제어를 멈추지 않는다.** 런타임의 10Hz 루프가 부르는 쓰기는 실패를
`history_write_failed` 로 남기고 `False`·`None` 을 돌려준다 — 블랙박스와 같은 규칙이다.
읽기와 검토 저장은 관제 서버가 부르므로 오류를 그대로 올린다.

연결은 둘이다 — 런타임이 쓰는 쓰기 연결과 관제 서버가 읽는 읽기 연결. WAL 이라 읽기가
쓰기를 기다리지 않는다. 여러 스레드가 부르므로 연결마다 잠금을 둔다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from host.common.config import repo_path
from host.common.logging_setup import event_logger

if TYPE_CHECKING:
    from host.common.blackbox import BlackboxEntry

LOG = event_logger("mechadog.history")
T = TypeVar("T")

SCHEMA_VERSION = "1"
#: `last_seen_at` 을 다시 쓰기까지의 최소 간격(ms). 텔레메트리는 10Hz 라 매번 쓰지 않는다.
SEEN_WRITE_MS = 5000
#: 한 번에 돌려주는 최대 행 수.
MAX_PAGE = 500
#: 검토 메모의 최대 길이 — 관제 화면 검토 양식과 같다.
MAX_RESOLUTION = 2000
#: 순찰 실행이 끝난 사유. `interrupted` 는 다음 기동 때 열린 채 발견된 실행이다.
RUN_RESULTS = frozenset({"stopped", "manual", "failsafe", "shutdown", "interrupted"})
#: 사건 전문의 공통 필드 — 나머지는 `detail` 로 간다. `type`·`seq` 는 관제 버퍼가 붙인다.
_FEED_KEYS = frozenset({"event", "ts_ms", "state", "escalation", "mode", "type", "seq"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS robots (
    robot_id TEXT PRIMARY KEY,
    serial_no TEXT,
    display_name TEXT NOT NULL,
    status TEXT,
    last_seen_at INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    zone_name TEXT NOT NULL,
    ppe_policy TEXT NOT NULL CHECK (ppe_policy IN ('helmet+vest', 'helmet', 'vest', 'none')),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('hazard', 'normal')),
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS mission_runs (
    mission_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES robots(robot_id),
    mode TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    result TEXT CHECK (
        result IS NULL OR result IN ('stopped', 'manual', 'failsafe', 'shutdown', 'interrupted')
    ),
    zones_visited TEXT NOT NULL DEFAULT '[]',
    incident_count INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_robot_started ON mission_runs(robot_id, started_at);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    mission_id TEXT REFERENCES mission_runs(mission_id),
    robot_id TEXT NOT NULL REFERENCES robots(robot_id),
    zone_id TEXT REFERENCES zones(zone_id),
    occurred_at INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    mode TEXT,
    state TEXT,
    escalation_level TEXT,
    confidence REAL,
    snapshot_path TEXT,
    blackbox_entry TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    resolution TEXT NOT NULL DEFAULT '',
    reviewed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(occurred_at);
CREATE INDEX IF NOT EXISTS idx_incidents_zone ON incidents(zone_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_incidents_event ON incidents(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_incidents_mission ON incidents(mission_id);
"""


@dataclass(frozen=True, slots=True)
class ZoneRow:
    zone_id: str
    zone_name: str
    #: `helmet+vest` · `helmet` · `vest` · `none` — 그 구역에서 요구하는 보호구.
    ppe_policy: str
    #: `hazard`(화기 위험구역 · `zones.hazard_ids`) 또는 `normal`.
    risk_level: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class IncidentRow:
    incident_id: str
    robot_id: str
    occurred_at: int
    event_type: str
    mission_id: str | None = None
    zone_id: str | None = None
    mode: str | None = None
    state: str | None = None
    escalation_level: str | None = None
    #: 그 사건 사진의 검출·추적 점수 중 가장 높은 값. 점수가 없는 사건은 `None`.
    confidence: float | None = None
    #: 블랙박스 폴더 기준 상대 경로(`<entry>/snapshot.jpg`). 절대 경로는 남기지 않는다.
    snapshot_path: str | None = None
    blackbox_entry: str | None = None
    #: 판단 근거(블랙박스 `judgement`) 또는 사건 전문의 나머지 필드.
    detail: Mapping[str, Any] = field(default_factory=dict)


def zones_from_config(config: Mapping[str, Any]) -> list[ZoneRow]:
    """설정의 구역 목록을 DB 행으로. 정책이 없는 항목은 필수다 (`zone_policy.DEFAULT_PPE`)."""
    zones = config.get("zones") or {}
    hazard = set(zones.get("hazard_ids") or ())
    policies = zones.get("policies") or {}
    rows: list[ZoneRow] = []
    for zone_id in zones.get("ids") or ():
        policy = policies.get(zone_id) or {}
        helmet = bool(policy.get("helmet", True))
        vest = bool(policy.get("vest", True))
        name = policy.get("name")
        note = policy.get("note")
        rows.append(
            ZoneRow(
                zone_id=str(zone_id),
                zone_name=name if isinstance(name, str) and name.strip() else str(zone_id),
                ppe_policy=(
                    "helmet+vest"
                    if helmet and vest
                    else "helmet"
                    if helmet
                    else "vest"
                    if vest
                    else "none"
                ),
                risk_level="hazard" if zone_id in hazard else "normal",
                description=note if isinstance(note, str) else "",
            )
        )
    return rows


def _best_score(*groups: Iterable[Mapping[str, Any]]) -> float | None:
    scores = [
        float(item["score"])
        for group in groups
        for item in group
        if isinstance(item.get("score"), int | float) and not isinstance(item.get("score"), bool)
    ]
    return max(scores) if scores else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def incident_from_meta(
    entry_name: str,
    meta: Mapping[str, Any],
    *,
    robot_id: str,
    mission_id: str | None = None,
    zone_id: str | None = None,
    snapshot_name: str | None = None,
) -> IncidentRow:
    """블랙박스 폴더 하나(`meta.json`)를 사건 행으로. 가져오기 도구도 이것을 쓴다.

    구역은 판단 근거에 적힌 것이 먼저다 — 구역 판독은 그 구역에서 건 것이라, 결과가
    도착했을 때 로봇이 이미 떠났어도 사건은 그 구역의 것이다.
    """
    judgement = meta.get("judgement")
    detail = dict(judgement) if isinstance(judgement, Mapping) else {}
    judged_zone = detail.get("zone")
    return IncidentRow(
        incident_id=entry_name,
        robot_id=robot_id,
        occurred_at=int(meta["ts_ms"]),
        event_type=str(meta["event"]),
        mission_id=mission_id,
        zone_id=judged_zone if isinstance(judged_zone, str) and judged_zone else zone_id,
        mode=_text(meta.get("mode")),
        state=_text(meta.get("state")),
        escalation_level=_text(meta.get("escalation")),
        confidence=_best_score(meta.get("tracks") or (), meta.get("detections") or ()),
        snapshot_path=None if snapshot_name is None else f"{entry_name}/{snapshot_name}",
        blackbox_entry=entry_name,
        detail=detail,
    )


def incident_from_entry(
    entry: BlackboxEntry, *, robot_id: str, mission_id: str | None, zone_id: str | None
) -> IncidentRow:
    """방금 남긴 블랙박스 기록을 사건 행으로. 사건 ID 는 블랙박스 폴더 이름이다."""
    meta = {
        "ts_ms": entry.ts_ms,
        "event": entry.event_type,
        "state": entry.state,
        "escalation": entry.escalation,
        "mode": entry.mode,
        "tracks": entry.tracks,
        "detections": entry.detections,
        "judgement": entry.judgement,
    }
    return incident_from_meta(
        entry.meta_path.parent.name,
        meta,
        robot_id=robot_id,
        mission_id=mission_id,
        zone_id=zone_id,
        snapshot_name=None if entry.jpeg_path is None else entry.jpeg_path.name,
    )


def incident_from_feed(
    payload: Mapping[str, Any], *, robot_id: str, mission_id: str | None, zone_id: str | None
) -> IncidentRow:
    """사진 없는 관제 사건(단계 변화·인증·페일세이프)을 사건 행으로.

    ⚠️ **같은 밀리초에 같은 사건이 둘 나올 수 있다** — ID 에 난수를 붙여 둘 다 남긴다.
    """
    event = str(payload["event"])
    occurred = int(payload["ts_ms"])
    return IncidentRow(
        incident_id=f"{occurred}_{event}_{uuid4().hex[:8]}",
        robot_id=robot_id,
        occurred_at=occurred,
        event_type=event,
        mission_id=mission_id,
        zone_id=zone_id,
        mode=_text(payload.get("mode")),
        state=_text(payload.get("state")),
        escalation_level=_text(payload.get("escalation")),
        detail={key: value for key, value in payload.items() if key not in _FEED_KEYS},
    )


def _check_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit 은 1~{MAX_PAGE} 정수여야 함")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset 은 0 이상 정수여야 함")


def _incident_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["detail"] = json.loads(data["detail"])
    data["reviewed"] = bool(data["reviewed"])
    return data


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["zones_visited"] = json.loads(data["zones_visited"])
    return data


def _active_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["active"] = bool(data["active"])
    return data


class HistoryStore:
    """SQLite 파일 하나. 런타임이 쓰고 관제 서버가 읽는다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        #: 기체별 마지막으로 쓴 (시각, 상태) — `note_seen` 의 쓰기 간격.
        self._seen: dict[str, tuple[int, str]] = {}
        self._writer = self._connect()
        try:
            self._migrate(self._writer)
            self._reader = self._connect()
        except BaseException:
            self._writer.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        # 자동 커밋으로 열고 쓰기는 `_transaction` 이 직접 BEGIN·COMMIT 한다.
        conn = sqlite3.connect(
            self.path, timeout=2.0, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """⚠️ 모르는 판의 DB 는 고쳐 쓰지 않는다 — 열기를 거부한다."""
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
        ).fetchone()
        if found is not None:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            if row is not None and row[0] != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"history schema version {row[0]} != {SCHEMA_VERSION}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('version', ?) ON CONFLICT(key) DO NOTHING",
            (SCHEMA_VERSION,),
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._writer
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _write(self, op: str, work: Callable[[sqlite3.Connection], T]) -> T | None:
        try:
            with self._transaction() as conn:
                return work(conn)
        except sqlite3.Error as exc:
            LOG.error("history_write_failed", op=op, error=f"{type(exc).__name__}: {exc}")
            return None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._reader.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._write_lock:
            self._writer.close()
        with self._read_lock:
            self._reader.close()

    # ── 런타임이 부르는 쓰기 (실패는 기록만 하고 삼킨다) ─────────────────

    def upsert_robot(self, robot_id: str, *, serial_no: str | None) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                "INSERT INTO robots(robot_id, serial_no, display_name) VALUES (?, ?, ?) "
                "ON CONFLICT(robot_id) DO UPDATE SET serial_no = excluded.serial_no, active = 1",
                (robot_id, serial_no, robot_id),
            )
            return True

        return bool(self._write("upsert_robot", work))

    def sync_zones(self, rows: Iterable[ZoneRow]) -> bool:
        """설정의 구역으로 맞춘다. 설정에서 빠진 구역은 지우지 않고 쉬게 한다 — 지난 사건이 가리킨다."""
        zones = list(rows)

        def work(conn: sqlite3.Connection) -> bool:
            conn.executemany(
                "INSERT INTO zones(zone_id, zone_name, ppe_policy, risk_level, description, active) "
                "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(zone_id) DO UPDATE SET "
                "zone_name = excluded.zone_name, ppe_policy = excluded.ppe_policy, "
                "risk_level = excluded.risk_level, description = excluded.description, active = 1",
                [
                    (z.zone_id, z.zone_name, z.ppe_policy, z.risk_level, z.description)
                    for z in zones
                ],
            )
            marks = ", ".join("?" for _ in zones)
            keep = f" WHERE zone_id NOT IN ({marks})" if zones else ""
            conn.execute(f"UPDATE zones SET active = 0{keep}", [z.zone_id for z in zones])
            return True

        return bool(self._write("sync_zones", work))

    def note_seen(self, robot_id: str, *, at_ms: int, status: str) -> bool:
        """마지막 수신 시각과 상태. 상태가 그대로면 `SEEN_WRITE_MS` 안에서는 다시 쓰지 않는다."""
        last = self._seen.get(robot_id)
        if last is not None and last[1] == status and at_ms - last[0] < SEEN_WRITE_MS:
            return False

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                "UPDATE robots SET last_seen_at = ?, status = ? WHERE robot_id = ?",
                (at_ms, status, robot_id),
            )
            return True

        written = bool(self._write("note_seen", work))
        if written:
            self._seen[robot_id] = (at_ms, status)
        return written

    def open_run(self, robot_id: str, *, mode: str, started_at: int) -> str | None:
        mission_id = f"{robot_id}-{started_at}"

        def work(conn: sqlite3.Connection) -> str:
            conn.execute(
                "INSERT INTO mission_runs(mission_id, robot_id, mode, started_at) "
                "VALUES (?, ?, ?, ?)",
                (mission_id, robot_id, mode, started_at),
            )
            return mission_id

        return self._write("open_run", work)

    def visit_zone(self, mission_id: str, zone_id: str) -> bool:
        """처음 도착한 순서대로 한 번씩만 남긴다."""

        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT zones_visited FROM mission_runs WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                return False
            visited = json.loads(row[0])
            if zone_id not in visited:
                conn.execute(
                    "UPDATE mission_runs SET zones_visited = ? WHERE mission_id = ?",
                    (json.dumps([*visited, zone_id], ensure_ascii=False), mission_id),
                )
            return True

        return bool(self._write("visit_zone", work))

    def close_run(
        self, mission_id: str, *, ended_at: int, result: str, stop_reason: str | None
    ) -> bool:
        if result not in RUN_RESULTS:
            raise ValueError(f"알 수 없는 순찰 결과: {result}")

        def work(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE mission_runs SET ended_at = ?, result = ?, stop_reason = ? "
                "WHERE mission_id = ? AND ended_at IS NULL",
                (ended_at, result, stop_reason, mission_id),
            )
            return cursor.rowcount == 1

        return bool(self._write("close_run", work))

    def close_open_runs(
        self,
        robot_id: str,
        *,
        ended_at: int,
        result: str = "interrupted",
        stop_reason: str | None = None,
    ) -> int:
        """이 기체의 열린 실행을 모두 닫는다 — 기동 때는 지난 비정상 종료의 흔적이다."""
        if result not in RUN_RESULTS:
            raise ValueError(f"알 수 없는 순찰 결과: {result}")

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE mission_runs SET ended_at = ?, result = ?, stop_reason = ? "
                "WHERE robot_id = ? AND ended_at IS NULL",
                (ended_at, result, stop_reason, robot_id),
            )
            return cursor.rowcount

        return self._write("close_open_runs", work) or 0

    def record_incident(self, row: IncidentRow) -> bool:
        """사건 한 건. 같은 ID 는 한 번만 들어간다. 새로 들어갔으면 `True`.

        ⚠️ **모르는 구역·기체 때문에 사건을 잃지 않는다** — 자리표 행을 만들고 넣는다.
        """

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute(
                "INSERT INTO robots(robot_id, display_name) VALUES (?, ?) "
                "ON CONFLICT(robot_id) DO NOTHING",
                (row.robot_id, row.robot_id),
            )
            if row.zone_id is not None:
                conn.execute(
                    "INSERT INTO zones(zone_id, zone_name, ppe_policy, risk_level) "
                    "VALUES (?, ?, 'helmet+vest', 'normal') ON CONFLICT(zone_id) DO NOTHING",
                    (row.zone_id, row.zone_id),
                )
            mission = row.mission_id
            if (
                mission is not None
                and conn.execute(
                    "SELECT 1 FROM mission_runs WHERE mission_id = ?", (mission,)
                ).fetchone()
                is None
            ):
                mission = None
            cursor = conn.execute(
                "INSERT INTO incidents(incident_id, mission_id, robot_id, zone_id, occurred_at, "
                "event_type, mode, state, escalation_level, confidence, snapshot_path, "
                "blackbox_entry, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(incident_id) DO NOTHING",
                (
                    row.incident_id,
                    mission,
                    row.robot_id,
                    row.zone_id,
                    row.occurred_at,
                    row.event_type,
                    row.mode,
                    row.state,
                    row.escalation_level,
                    row.confidence,
                    row.snapshot_path,
                    row.blackbox_entry,
                    json.dumps(dict(row.detail), ensure_ascii=False, default=str),
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted and mission is not None:
                conn.execute(
                    "UPDATE mission_runs SET incident_count = incident_count + 1 "
                    "WHERE mission_id = ?",
                    (mission,),
                )
            return inserted

        return bool(self._write("record_incident", work))

    # ── 관제 서버가 부르는 읽기·검토 (오류를 올린다) ────────────────────

    def review(
        self, incident_id: str, *, reviewed: bool, resolution: str, at_ms: int
    ) -> dict[str, Any] | None:
        """검토 여부와 처리 메모. 없는 사건이면 `None`."""
        if not isinstance(reviewed, bool):
            raise ValueError("reviewed 는 참·거짓이어야 함")
        if not isinstance(resolution, str) or len(resolution) > MAX_RESOLUTION:
            raise ValueError(f"resolution 은 {MAX_RESOLUTION}자 이하 문자열이어야 함")
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE incidents SET reviewed = ?, resolution = ?, reviewed_at = ? "
                "WHERE incident_id = ?",
                (int(reviewed), resolution, at_ms if reviewed else None, incident_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.incident(incident_id)

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        return _incident_dict(rows[0]) if rows else None

    def incidents(
        self,
        *,
        since: int | None = None,
        until: int | None = None,
        robot: str | None = None,
        zone: str | None = None,
        event: str | None = None,
        escalation: str | None = None,
        mission: str | None = None,
        reviewed: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """조건에 맞는 사건(최근 것부터)과 전체 건수. 시각 범위는 양 끝을 포함한다."""
        _check_page(limit, offset)
        clauses: list[str] = []
        params: list[Any] = []
        for column, op, value in (
            ("occurred_at", ">=", since),
            ("occurred_at", "<=", until),
            ("robot_id", "=", robot),
            ("zone_id", "=", zone),
            ("event_type", "=", event),
            ("escalation_level", "=", escalation),
            ("mission_id", "=", mission),
            ("reviewed", "=", None if reviewed is None else int(reviewed)),
        ):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(self._query(f"SELECT COUNT(*) FROM incidents{where}", params)[0][0])
        rows = self._query(
            f"SELECT * FROM incidents{where} ORDER BY occurred_at DESC, incident_id DESC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [_incident_dict(row) for row in rows], total

    def runs(
        self, *, robot: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """순찰 실행(최근 것부터)과 전체 건수."""
        _check_page(limit, offset)
        where, params = (" WHERE robot_id = ?", [robot]) if robot is not None else ("", [])
        total = int(self._query(f"SELECT COUNT(*) FROM mission_runs{where}", params)[0][0])
        rows = self._query(
            f"SELECT * FROM mission_runs{where} ORDER BY started_at DESC, mission_id DESC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [_run_dict(row) for row in rows], total

    def robots(self) -> list[dict[str, Any]]:
        return [_active_dict(row) for row in self._query("SELECT * FROM robots ORDER BY robot_id")]

    def zones(self) -> list[dict[str, Any]]:
        return [_active_dict(row) for row in self._query("SELECT * FROM zones ORDER BY zone_id")]


def open_history(config: Mapping[str, Any]) -> HistoryStore | None:
    """설정(`logging.history_db`)대로 연다. 비었거나 열 수 없으면 `None` — DB 없이 운용한다.

    이 DB 는 색인이라 없어도 블랙박스와 JSONL 은 그대로 남는다. 기동을 막을 이유가 없다.
    """
    value = (config.get("logging") or {}).get("history_db")
    if not isinstance(value, str) or not value.strip():
        LOG.info("history_disabled")
        return None
    path = repo_path(value)
    try:
        return HistoryStore(path)
    except (sqlite3.Error, OSError) as exc:
        LOG.error("history_unavailable", path=str(path), error=f"{type(exc).__name__}: {exc}")
        return None
