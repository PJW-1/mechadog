"""운영값 조회와 신선도 계약 (WBS 4.7.17 · FR-12.2~12.6 · ADR-34).

`store.py` 가 «읽는 문» 이라면 이 모듈은 «답해도 되는가» 를 정하는 자리다. 조회군
이름을 표 이름으로 바꾸고, 라인 필터를 검사하고, 행마다 신선도를 붙인다.

⚠️ **TTL 판정을 답변 문구가 아니라 자료 구조에서 한다** (DoD ②③). 행에 `fresh` 를
붙여 두면 *"모르면 모른다고 한다"* 가 문장을 쓰는 사람의 성실함이 아니라 코드의
성질이 된다. 라우터는 `fresh` 가 아닌 행을 답에 넣을 방법이 없다.

⚠️ **낡은 값은 추정으로 메우지 않는다** (ADR-34 운용규칙 4). 한 건도 신선하지
않으면 조회는 실패이고, 「최신 정보를 확인할 수 없습니다」로 닫는다. 마지막으로
읽은 값을 대신 말하는 순간 TTL 을 둔 이유가 사라진다.

⚠️ **미래 시각도 낡은 것으로 친다.** 원본의 시계가 앞서 있으면 `updated_at` 이
미래가 되고, 나이를 절댓값 없이 재면 그 행은 영원히 신선하다. TTL 이 아무것도
막지 못하는 상태가 조용히 생긴다.

⚠️ **«없는 라인» 과 «기록 0건» 을 다른 답으로 만든다.** 둘을 같은 값으로 돌리면
「D라인 작업 있나요」에 *"없습니다"* 가 나가고, 묻는 사람은 D라인이 한가한 줄
안다. 등록되지 않은 이름은 `UnknownLineError` 로 끊는다.

⚠️ **범위 조건과 정렬은 파이썬에서 한다.** `store.read()` 가 받는 것은 화이트리스트
칸의 동등 비교뿐이고, `4.7.15` 가 SQL 표면을 그만큼으로 좁혀 둔 것은 의도다. 납기
범위를 위해 거기에 부등호를 더하면 좁혀 둔 자리가 다시 넓어진다. 표 하나가 라인
단위 행 수라서 읽고 거르는 비용이 문제가 되지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from host.common.config import ConfigError, load_base_config, repo_path
from host.factory_ops.store import FactoryOps, Reading, StoreError

__all__ = [
    "DEFAULT_TTL_S",
    "ENDPOINTS",
    "KST",
    "Service",
    "SourceError",
    "UnknownLineError",
    "mark_freshness",
    "now",
]

#: 현장 시각. `seed.py` 와 같은 값이지만 그쪽을 import 하지 않는다 — 쓰기 모듈을
#: 응용 경로가 부르지 않는 것이 `4.7.15` DoD ④ 의 구현이기 때문이다.
KST = timezone(timedelta(hours=9))

#: 조회군 → 표. **라우터는 표 이름을 모른다.** 질의 규칙이 스키마를 직접 가리키면
#: 표 이름을 고칠 때 규칙표까지 따라 고쳐야 하고, 그 둘은 개정 이유가 다르다.
ENDPOINTS: Mapping[str, str] = {
    "production": "production_status",
    "shipments": "shipment_schedule",
    "schedule": "work_schedule",
    "equipment": "equipment_status",
}

#: 설정에 항목별 TTL 이 없을 때 쓰는 값. **설정이 정본이고 이것은 안전망이다.**
DEFAULT_TTL_S = 6 * 3600

#: 출하 조회가 기본으로 보는 앞날의 길이(일).
DEFAULT_SHIPMENT_DAYS = 7

#: 라인 이름이 실리는 표들. `known_lines()` 가 여기서 등록된 라인을 모은다.
_LINE_TABLES = ("production_status", "work_schedule", "equipment_status")


class SourceError(ConfigError):
    """운영 자료를 줄 수 없음: DB 없음·스키마 어긋남·모르는 조회군.

    ⚠️ **`ConfigError` 를 물려받는다.** 뜻이 *"이 설정으로는 답할 수 없다"* 로
    `mission.ModeError` 와 같아서, 런타임의 설정 오류 경로가 `except` 절을 새로
    두지 않아도 이것을 함께 잡는다.
    """


class UnknownLineError(SourceError):
    """등록되지 않은 라인. **조회 실패이지 «기록 없음» 이 아니다.**"""

    def __init__(self, line: str, valid: Sequence[str]) -> None:
        self.line = line
        self.valid = tuple(valid)
        listed = ", ".join(self.valid) or "없음"
        super().__init__(f"등록되지 않은 라인: {line} (등록: {listed})")


def now() -> datetime:
    return datetime.now(KST)


def mark_freshness(
    rows: list[dict[str, Any]], ttl_s: float, *, source: str, at: datetime | None = None
) -> list[dict[str, Any]]:
    """행마다 `source` 와 `fresh` 를 붙인다 (DoD ②).

    `updated_at` 을 읽을 수 없는 행은 신선하지 않은 것으로 친다. 읽지 못한 것과
    낡은 것은 둘 다 «지금 값이라고 말할 수 없다» 이고, 그 둘을 가르는 이득이 없다.
    """
    at = at or now()
    for row in rows:
        row["source"] = source
        try:
            age = (at - datetime.fromisoformat(str(row["updated_at"]))).total_seconds()
        except (KeyError, TypeError, ValueError):
            row["fresh"] = False
            continue
        # 0 보다 작은 나이는 원본 시계가 앞선 것이다 — 신선하다고 보지 않는다.
        row["fresh"] = 0 <= age <= ttl_s
    return rows


class Service:
    """읽기 전용 조회 + 신선도. **라우터가 쓰는 provider 가 이것이다.**"""

    def __init__(
        self, ops: FactoryOps | None = None, ttl_s: Mapping[str, Any] | None = None
    ) -> None:
        self.ops = ops if ops is not None else FactoryOps()
        self.ttl_s = dict(ttl_s or {})

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None = None) -> Service:
        """`config.yaml` 의 `factory_ops` 절로 만든다.

        ⚠️ **이 절을 `REQUIRED_SECTIONS` 에 넣지 않았다.** 넣으면 현장지원을 쓰지
        않는 경비 운용까지 기동이 막힌다. `lidar` 절과 같은 판단이다.
        """
        config = config if config is not None else load_base_config()
        section = config.get("factory_ops")
        if not isinstance(section, dict):
            raise SourceError("config.yaml 에 factory_ops 절이 없다 — ADR-34 가 이 절을 요구한다")
        raw = section.get("db_path")
        if not isinstance(raw, str) or not raw.strip():
            raise SourceError("factory_ops.db_path 는 비어 있지 않은 문자열이어야 한다")
        ttl = section.get("ttl_s")
        return cls(
            FactoryOps(db_path=repo_path(raw.strip())),
            ttl if isinstance(ttl, dict) else {},
        )

    def ttl_for(self, endpoint: str) -> float:
        """항목별 TTL (DoD ①). 적히지 않았거나 읽을 수 없으면 기본값을 쓴다."""
        try:
            return float(self.ttl_s[endpoint])
        except (KeyError, TypeError, ValueError):
            return DEFAULT_TTL_S

    def known_lines(self) -> tuple[str, ...]:
        """등록된 라인 ID. 「없는 라인」과 「기록 0건」을 가르는 근거다."""
        seen: set[str] = set()
        for table in _LINE_TABLES:
            for row in self._read(table).rows:
                seen.add(str(row["line_id"]))
        return tuple(sorted(seen))

    def query(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """조회군 → 행 목록. 모르는 조회군은 `SourceError` 다.

        ⚠️ **자연어가 SQL 에 닿지 않는다** (FR-12.2). 여기로 들어오는 것은 규칙표가
        고른 조회군 이름과 라인 ID 뿐이고, 라인 ID 는 등록 목록과 대조한 뒤에야
        `store.read()` 의 바인딩 파라미터가 된다.
        """
        table = ENDPOINTS.get(endpoint)
        if table is None:
            raise SourceError(f"모르는 조회군: {endpoint!r}")
        params = dict(params or {})
        at = at or now()

        line = str(params["line"]).upper() if params.get("line") else None
        if line is not None and line not in self.known_lines():
            raise UnknownLineError(line, self.known_lines())

        reading = self._read(table, **({"line_id": line} if line else {}))
        rows = [dict(row) for row in reading.rows]

        if endpoint == "production":
            for row in rows:
                row["remaining_quantity"] = max(
                    0, int(row["target_quantity"]) - int(row["completed_quantity"])
                )
        elif endpoint == "schedule":
            # 끝난 지시는 «앞으로 할 일» 이 아니다. 우선순위 순으로 답한다.
            rows = sorted(
                (r for r in rows if str(r["status"]) != "done"), key=lambda r: int(r["priority"])
            )
        elif endpoint == "shipments":
            days = self._days(params.get("days"))
            until = (at.date() + timedelta(days=days)).isoformat()
            rows = sorted(
                (r for r in rows if str(r["deadline"])[:10] <= until),
                key=lambda r: str(r["deadline"]),
            )

        return mark_freshness(rows, self.ttl_for(endpoint), source=reading.source, at=at)

    @staticmethod
    def _days(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_SHIPMENT_DAYS

    def _read(self, table: str, **filters: Any) -> Reading:
        """`store` 의 실패를 이 모듈의 실패로 바꾼다.

        부르는 쪽이 `StoreError` 와 `SourceError` 를 둘 다 알아야 할 이유가 없다.
        """
        try:
            return self.ops.read(table, **filters)
        except StoreError as exc:
            raise SourceError(f"운영 자료를 읽을 수 없다: {exc}") from exc
