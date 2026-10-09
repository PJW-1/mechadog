"""행동 계층 경계 — 순찰기 협력자는 남의 객체의 비공개 속성(`patrol._이름`)을 읽거나 쓰지 않는다.

`host/behavior/` 의 모듈마다 `X._y` 꼴 접근 중 `X` 가 `self`·`cls` 가 아닌 것을 센다
(`getattr`·`setattr`·`hasattr`·`delattr` 에 `"_..."` 문자열을 넘기는 것도 센다). 이중 밑줄
특수 이름(`__name__` 등)은 세지 않는다. 공유 항법 상태는 `PatrolNavState`
(`host/behavior/nav_state.py`) 로, 콜백은 `PatrolController` 의 공개 메서드로 오간다.

예외 — 같은 클래스의 다른 인스턴스(`zones.py` 의 `store._zones`)와 런타임 쪽 요청 처리
(`nav_requests.py` 의 `navigator._own_localization`·`_zone_filter`·`_point_hint`·`_zone_hint`)는
이 경계 밖이다.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEHAVIOR = ROOT / "host" / "behavior"

#: 파일별 허용 접근 (파일 이름, 받는 쪽 표기, 속성 이름) — 이 경계의 예외.
ALLOWED = frozenset(
    {
        ("zones.py", "store", "_zones"),
        ("nav_requests.py", "navigator", "_own_localization"),
        ("nav_requests.py", "navigator", "_zone_filter"),
        ("nav_requests.py", "navigator", "_point_hint"),
        ("nav_requests.py", "navigator", "_zone_hint"),
    }
)

#: 파일별 비공개 접근 수의 상한 — 줄이면 함께 낮춘다(래칫). 목록 밖 파일은 0 이어야 한다.
EXPECTED: dict[str, int] = {
    "arrival.py": 0,
    "avoidance.py": 0,
    "localization.py": 0,
    "nav_map.py": 0,
    "recovery.py": 55,
    "relaxed_follow.py": 0,
    "route_follow.py": 0,
}

_ATTR_FUNCS = frozenset({"getattr", "setattr", "hasattr", "delattr"})


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in {"self", "cls"}


def private_accesses(source: str) -> list[tuple[int, str, str]]:
    """`self`·`cls` 가 아닌 받는 쪽의 비공개 속성 접근 — (줄, 받는 쪽 표기, 속성 이름)."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and _is_private(node.attr) and not _is_self(node.value):
            found.append((node.lineno, ast.unparse(node.value), node.attr))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _ATTR_FUNCS
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _is_private(node.args[1].value)
            and not _is_self(node.args[0])
        ):
            found.append((node.lineno, ast.unparse(node.args[0]), node.args[1].value))
    return sorted(found)


def _counts() -> dict[str, list[tuple[int, str, str]]]:
    counts: dict[str, list[tuple[int, str, str]]] = {}
    for path in sorted(BEHAVIOR.glob("*.py")):
        hits = [
            hit
            for hit in private_accesses(path.read_text(encoding="utf-8"))
            if (path.name, hit[1], hit[2]) not in ALLOWED
        ]
        if hits:
            counts[path.name] = hits
    return counts


def test_the_scan_counts_attributes_and_string_attribute_calls() -> None:
    """검사기 자체 확인 — 남의 비공개 속성과 문자열 접근은 세고 `self`·특수 이름은 뺀다."""
    hits = private_accesses(
        "def f(self, patrol):\n"
        "    self._a = patrol._b\n"
        "    self._patrol._c()\n"
        "    getattr(patrol, '_d')\n"
        "    setattr(patrol, '_e', 1)\n"
        "    getattr(self, '_f')\n"
        "    patrol.__class__\n"
        "    patrol.public\n"
    )
    assert [(name, attr) for _line, name, attr in hits] == [
        ("patrol", "_b"),
        ("self._patrol", "_c"),
        ("patrol", "_d"),
        ("patrol", "_e"),
    ]


def test_behavior_modules_do_not_reach_into_private_attributes() -> None:
    counts = _counts()
    actual = {name: len(hits) for name, hits in counts.items()}
    detail = {
        name: [f"{line} {owner}.{attr}" for line, owner, attr in hits[:5]]
        for name, hits in counts.items()
        if actual[name] != EXPECTED.get(name, 0)
    }
    assert actual == {name: n for name, n in EXPECTED.items() if n}, detail
