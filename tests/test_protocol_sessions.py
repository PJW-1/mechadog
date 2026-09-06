"""재시작과 경계값에서 Python/C++ 명령 파서가 같은 판정을 하는지 확인한다."""

import json
from pathlib import Path

from host.common.protocol import CommandDecoder, Verdict


def test_session_fixture_matches_python_decoder() -> None:
    decoder = CommandDecoder()
    path = Path(__file__).parent / "fixtures" / "protocol_sessions.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        assert decoder.decode(line).verdict is Verdict(case["_expect"]), case
