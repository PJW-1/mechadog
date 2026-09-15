"""일자별 음성 이벤트 영속 기록 (WBS 4.7.12).

`Hub.event()` 가 메모리 deque 에 넣는 같은 줄을 날짜별 JSONL 파일에도 남긴다 —
메모리는 재시작하면 날아가지만 리포트는 파일에서 다시 계산할 수 있어야 한다.

⚠️ **쓰기 실패가 음성 루프를 멈추면 안 된다.** 디스크가 가득 차거나 경로가
잠겨도 마이크·스피커는 계속 돌아야 하므로, 실패는 콘솔에 한 줄 남기고 넘어간다.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class EventJournal:
    """날짜별 `voice-YYYY-MM-DD.jsonl` 에 한 줄씩 추가한다. 스레드 안전."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._date = ""
        self._fh = None

    def path_for(self, date: str) -> Path:
        return self.dir / f"voice-{date}.jsonl"

    def record(self, role: str, text: str) -> None:
        line = json.dumps(
            {"ts": time.strftime("%H:%M:%S"), "role": role, "text": text},
            ensure_ascii=False,
        )
        try:
            with self._lock:
                fh = self._file()
                fh.write(line + "\n")
                fh.flush()  # 재시작·크래시에도 마지막 줄까지 남긴다
        except OSError as exc:
            print(f"[journal] write failed: {exc}")

    def _file(self):
        """오늘 날짜의 파일 핸들. 자정을 넘기면 새 파일로 바꾼다."""
        today = time.strftime("%Y-%m-%d")
        if self._fh is None or self._date != today:
            self.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._date = today
            self._fh = self.path_for(today).open("a", encoding="utf-8")
        return self._fh

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def load_events(path) -> list[dict]:
    """JSONL 파일을 읽는다. 깨진 줄은 건너뛰고, 파일이 없으면 빈 목록."""
    events = []
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 크래시로 반쪽 난 줄은 버린다
                if isinstance(event, dict) and "role" in event:
                    events.append(event)
    except OSError:
        return []
    return events
