"""콘솔 출력이 인코딩 때문에 죽지 않게 한다.

⚠️ **한국어 Windows 콘솔은 cp949 다.** `print()` 가 `—`(U+2014)나 `⚠️`(U+26A0) 처럼
cp949 에 없는 문자를 만나면 `UnicodeEncodeError` 로 **그 줄에서 프로그램이 죽는다.**
한글은 cp949 에 있으므로 문제가 되는 것은 기호와 이모지다.

**이 함정을 세 번 밟았다.**

  ① `tools/fetch_models.py` — 첫 실행에서 `—` 로 죽었다
  ② `host/runtime.py` 의 콘솔 안내 배너 — `—` 로 **기동 자체가 실패했다**
  ③ `tools/gait_calibrate.py` — `⚠️` 로 죽었다

세 번째까지 밟은 이유는 분명하다 — 처방이 `fetch_models.py` 안의 사적 함수로만
있어서 다음 도구가 그것을 모른다. 그래서 여기로 옮겼다.

⚠️ **`ruff` 도 `pytest` 도 이것을 잡지 못한다** — 시험은 `print` 를 지나가지 않고
CI 러너는 UTF-8 이다. **개발 PC 에서만 죽는다.**

⚠️ **`tools/fetch_models.py` 는 같은 함수를 자기 안에 따로 들고 있다. 의도다** —
그 도구는 가중치를 받는 부트스트랩이라 **`host/` 를 import 하지 않는 유일한 도구**
이고(표준 라이브러리만 쓴다), 여기에 의존시키면 가중치를 받기 전 단계가 저장소
구조에 묶인다. 사본 둘의 대가보다 그쪽이 크다.
"""

from __future__ import annotations

import sys


def survive_encoding_errors() -> None:
    """표준 출력·오류의 인코딩 실패를 **치환으로 낮춘다.**

    글자가 물음표로 나오는 것이 프로그램이 죽는 것보다 낫다 — **검증 도구가 출력
    때문에 죽으면 검증을 못 한다.**

    ⚠️ 이것은 CLI 도구를 위한 처방이다. 로거는 이미 살아남으므로(콘솔 핸들러가
    치환한다) 운용 코드에서 부를 필요가 없다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
