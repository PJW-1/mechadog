"""명령줄 진입점 단독 검증 — `host.runtime_cli` (Runtime 분할 8단계).

기동 순서·설정 거부·LiDAR 관문 같은 시나리오는 `test_runtime.py`·`test_runtime_lidar.py`·
`test_dashboard.py` 가 `main()` 으로 본다. 여기서는 옮긴 뒤에도 바깥에서 보이는 진입점이
같은지만 본다 — `python -m host.runtime` 표기, `host.runtime.main` 위임, 콘솔 키.
"""

from __future__ import annotations

import io

import pytest

import host.runtime
import host.runtime_cli
from host.runtime_cli import CONSOLE_HELP, build_parser, watch_console


def test_parser_keeps_the_module_entry_name() -> None:
    """도움말에 찍히는 실행 이름은 옮기기 전과 같은 `python -m host.runtime` 이다."""
    assert build_parser().prog == "python -m host.runtime"


def test_runtime_main_forwards_to_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mechadog-runtime` 스크립트와 `python -m host.runtime` 이 부르는 `host.runtime.main`."""
    seen: list[list[str] | None] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(argv)
        return 7

    monkeypatch.setattr(host.runtime_cli, "main", fake_main)
    assert host.runtime.main(["--device", "x"]) == 7
    assert seen == [["--device", "x"]]


def test_console_keys_only_queue_requests() -> None:
    class Asked:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def ask_alarm_confirm(self) -> None:
            self.calls.append("confirm")

        def ask_reset(self) -> None:
            self.calls.append("reset")

    asked = Asked()
    watch_console(asked, io.StringIO("c\n R \nx\n"))  # type: ignore[arg-type]
    assert asked.calls == ["confirm", "reset"]
    # 개발 콘솔(cp949)에서 기동이 죽지 않게 배너는 cp949 로 인코딩된다.
    CONSOLE_HELP.encode("cp949")
