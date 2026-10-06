"""사건 추적 식별자 — 세션 ID 와 설정 해시.

세션 ID 는 런타임이 시작될 때마다 하나 만든다. 세션 기록기(`--record-dir`)의 manifest 와
블랙박스 `meta.json` 이 같은 값을 쓰므로, 기록기가 꺼진 실행에서도 사건끼리 같은
세션인지 가려낼 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4


def new_session_id() -> str:
    """사람이 읽을 수 있는 시작 시각(로컬) + 충돌 방지용 8자리 난수."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"


def config_sha256(config: Mapping[str, Any]) -> str:
    """설정 전체의 해시. 비밀값을 남기지 않으려고 내용 대신 해시만 기록한다."""
    text = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
