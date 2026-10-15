"""모듈 경계 — 아래 계층은 `host.behavior` 를 모르고, 남의 모듈은 런타임 내부를 읽지 않는다.

① `host.common`·`host.telemetry`·`host.slam`·`host.vision`·`host.cloud` 는 행동 계층
   (`host.behavior`) 을 가져오지 않는다. 모듈 맨 위·함수 안의 `import`·`from` 둘 다 센다.
   **`if TYPE_CHECKING:` 블록 안의 가져오기만 허용한다** — 실행 때는 읽히지 않는 타입 주석용이라
   실행 의존이 생기지 않는다 (`telemetry_watch` 의 `Behavior`, `vision_recording` 의 `Mission`).
② `host/runtime.py` 밖의 모듈은 `runtime._이름` 처럼 런타임의 비공개 속성을 읽지 않는다 —
   공개 속성(`Runtime.navigator` 등)으로 읽는다. 시험은 이 규칙 밖이다.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "host"

#: `host.behavior` 에 기대면 안 되는 아래 계층 패키지.
LOWER_PACKAGES = ("common", "telemetry", "slam", "vision", "cloud")


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """`if TYPE_CHECKING:` 본문을 뺀 모든 가져오기 — (줄, 모듈 이름)."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _modules(package: str) -> list[Path]:
    return sorted((HOST / package).rglob("*.py"))


def test_lower_packages_exist() -> None:
    for package in LOWER_PACKAGES:
        assert _modules(package), f"host/{package} 에 모듈이 없다 — 목록을 고쳐야 한다"


def test_lower_layers_do_not_import_behavior() -> None:
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line} {name}"
        for package in LOWER_PACKAGES
        for path in _modules(package)
        for line, name in _runtime_imports(ast.parse(path.read_text(encoding="utf-8")))
        if name == "host.behavior" or name.startswith("host.behavior.")
    ]
    assert offenders == []


def test_the_import_scan_sees_function_level_and_both_forms() -> None:
    """검사기 자체 확인 — 함수 안 가져오기·두 형태는 잡고 `TYPE_CHECKING` 블록은 뺀다."""
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "import host.behavior.fsm\n"
        "if TYPE_CHECKING:\n"
        "    from host.behavior.mission import Mission\n"
        "def f():\n"
        "    from host.behavior.live_nav import NavParams\n"
    )
    names = [name for _line, name in _runtime_imports(tree) if name.startswith("host.behavior")]
    assert names == ["host.behavior.fsm", "host.behavior.live_nav.NavParams"]


def test_no_module_reads_private_runtime_attributes() -> None:
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{node.lineno} runtime.{node.attr}"
        for path in sorted(HOST.rglob("*.py"))
        if path != HOST / "runtime.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "runtime"
        and node.attr.startswith("_")
    ]
    getattr_offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
        for path in sorted(HOST.rglob("*.py"))
        if path != HOST / "runtime.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "runtime"
        and isinstance(node.args[1], ast.Constant)
        and str(node.args[1].value).startswith("_")
    ]
    assert offenders + getattr_offenders == []
