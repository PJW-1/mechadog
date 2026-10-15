"""콘솔 출력이 인코딩 때문에 죽지 않게 한다.

한국어 Windows 콘솔은 cp949 라 `—`(U+2014)·U+26A0 같은 기호를 `print()` 하면
`UnicodeEncodeError` 로 죽는다. CI 러너는 UTF-8 이라 시험이 잡지 못한다.

`tools/fetch_models.py` 는 `host/` 를 import 하지 않는 부트스트랩이라 같은 함수를 따로 들고 있다.
"""

from __future__ import annotations

import sys


def survive_encoding_errors() -> None:
    """표준 출력·오류의 인코딩 실패를 치환(물음표)으로 낮춘다.

    CLI 도구용이다. 로거의 콘솔 핸들러는 이미 치환하므로 운용 코드에서 부를 필요가 없다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
