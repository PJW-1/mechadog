"""Hardware-facing UDP tools tested against a local fake ESP32 socket."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from types import TracebackType

import pytest

from tools import mechdog_command, udp_probe


class FakeEsp:
    def __init__(self, *, answer_ping: bool = True, stale_first: bool = False) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.answer_ping = answer_ping
        self.stale_first = stale_first
        self.safe_latched = True
        self.failsafe_count = 0
        self.last_valid_at: float | None = None
        self.move_arrivals: list[float] = []
        self._stale_sent = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeEsp:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, source = self.sock.recvfrom(2048)
            except TimeoutError:
                continue

            if raw.startswith(b"PING"):
                if self.answer_ping:
                    self.sock.sendto(b"ACK " + raw, source)
                continue

            try:
                message = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._reply(source, ok=False, seq=0, command_type="UNKNOWN", applied=False)
                continue

            now = time.monotonic()
            if (
                not self.safe_latched
                and self.last_valid_at is not None
                and now - self.last_valid_at > 0.3
            ):
                self.safe_latched = True
                self.failsafe_count += 1

            seq = int(message["seq"])
            command_type = str(message["type"])
            self.last_valid_at = now
            applied = True
            if command_type == "RESET_SAFE":
                self.safe_latched = False
            elif command_type == "ESTOP":
                if not self.safe_latched:
                    self.failsafe_count += 1
                self.safe_latched = True
            elif command_type == "MOVE":
                self.move_arrivals.append(now)
                applied = not self.safe_latched

            if self.stale_first and not self._stale_sent:
                self._stale_sent = True
                self._reply(source, ok=True, seq=-1, command_type="STOP", applied=True)
            self._reply(
                source,
                ok=True,
                seq=seq,
                command_type=command_type,
                applied=applied,
            )

    def _reply(
        self,
        source: tuple[str, int],
        *,
        ok: bool,
        seq: int,
        command_type: str,
        applied: bool,
    ) -> None:
        response = json.dumps(
            {
                "ok": ok,
                "seq": seq,
                "type": command_type,
                "applied": applied,
                "safe_latched": self.safe_latched,
                "failsafe_count": self.failsafe_count,
                "actuators": False,
            }
        ).encode()
        self.sock.sendto(response, source)


def test_udp_probe_success_and_stale_accounting(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    with FakeEsp() as esp:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "udp_probe.py",
                "127.0.0.1",
                "--port",
                str(esp.port),
                "--count",
                "3",
                "--interval",
                "0",
            ],
        )
        assert udp_probe.main() == 0

    output = capsys.readouterr().out
    assert "sent=3 received=3" in output
    assert "loss=0.0%" in output


def test_udp_probe_timeout(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    with FakeEsp(answer_ping=False) as esp:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "udp_probe.py",
                "127.0.0.1",
                "--port",
                str(esp.port),
                "--timeout",
                "0.02",
            ],
        )
        assert udp_probe.main() == 1

    output = capsys.readouterr().out
    assert "received=0" in output
    assert "timeout" in output


@pytest.mark.parametrize("option,value", [("--count", "0"), ("--interval", "-1")])
def test_udp_probe_rejects_invalid_counts(
    monkeypatch: pytest.MonkeyPatch, option: str, value: str
) -> None:
    monkeypatch.setattr(sys, "argv", ["udp_probe.py", "127.0.0.1", option, value])
    with pytest.raises(SystemExit):
        udp_probe.main()


def test_command_cli_safety_move_and_watchdog(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    with FakeEsp(stale_first=True) as esp:
        common = ["mechdog_command.py", "127.0.0.1", "--port", str(esp.port)]

        monkeypatch.setattr(sys, "argv", [*common, "safety"])
        assert mechdog_command.main() == 0

        monkeypatch.setattr(
            sys,
            "argv",
            [*common, "move", "--step", "10", "--duration", "0.25"],
        )
        assert mechdog_command.main() == 0

        move_count_before_watchdog = len(esp.move_arrivals)
        monkeypatch.setattr(
            sys,
            "argv",
            [*common, "watchdog", "--step", "10", "--duration", "0.25"],
        )
        assert mechdog_command.main() == 0

        move_times = esp.move_arrivals[:move_count_before_watchdog]
        gaps = [right - left for left, right in zip(move_times, move_times[1:], strict=False)]
        assert gaps
        assert max(gaps) < 0.2
        assert esp.safe_latched is True
        assert esp.failsafe_count >= 2

    output = capsys.readouterr().out
    assert "SAFETY RESULT" in output
    assert "MOVE RESULT" in output
    assert "WATCHDOG RESULT" in output


def test_command_cli_reports_timeout_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class TimeoutClient:
        def __init__(self, _host: str, _port: int, _timeout: float) -> None:
            pass

        def send(self, _command_type: str, **_fields: object) -> dict[str, object]:
            raise TimeoutError("timed out")

        def close(self) -> None:
            pass

    monkeypatch.setattr(mechdog_command, "Client", TimeoutClient)
    monkeypatch.setattr(sys, "argv", ["mechdog_command.py", "127.0.0.1", "safety"])

    assert mechdog_command.main() == 2
    assert "통신 실패" in capsys.readouterr().err
