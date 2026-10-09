"""시연 장소 3D SIM 장면 데이터(Release `house-sim-20261006`)를 받아 이 폴더에 푼다.

    python tools/house_sim/fetch_data.py
    cd tools/house_sim && node server.mjs      # http://127.0.0.1:8789/

해시가 맞지 않으면 풀지 않는다 (`tools/fetch_models.py` 와 같은 규칙).
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://github.com/PJW-1/mechadog/releases/download/house-sim-20261006/house-sim-data-20261006.zip"
SHA256 = "71eba8ff52ee1ae88625ae7a24875f7a0460efe78b1773500b54a49051c7c956"
HERE = Path(__file__).resolve().parent


def main() -> int:
    if (HERE / "data" / "scene.json").exists() and "--force" not in sys.argv:
        print("이미 있다 — 다시 받으려면 --force")
        return 0
    with urllib.request.urlopen(URL, timeout=120) as response:
        blob = response.read()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != SHA256:
        print(f"해시 불일치: {digest} != {SHA256}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            target = (HERE / name).resolve()
            if not str(target).startswith(str(HERE)):
                print(f"폴더 밖 경로 거부: {name}", file=sys.stderr)
                return 1
        archive.extractall(HERE)
    print(f"풀었다: {HERE} ({len(blob):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
