"""모델 가중치 받기·검증 (WBS 3.3.1).

**가중치는 저장소에 없다.** 용량이 크고(34MB) 우리가 만든 것도 아니므로 `models/` 는
gitignore 다. 그래서 새로 받은 사람은 누구든 같은 파일을 손으로 받아야 하는데,
**손으로 하면 두 가지가 반드시 빠진다** — 해시 검증과 출처 기록이다.

    python tools/fetch_models.py            # 없는 것만 받고 검증
    python tools/fetch_models.py --check     # 받지 않고 현재 파일만 검증
    python tools/fetch_models.py --force     # 있어도 다시 받는다

⚠️ **문서에 적은 절차는 지켜지지 않는다.** `curl` 명령을 README 에 적어 두면 사람은
그것만 복사하고 `sha256sum` 줄은 건너뛴다. 그래서 검증을 **선택할 수 없게** 한 곳에
묶었다. 펌웨어 백업을 해시로 보존한 것과 같은 이유다 — *복원해 본 적 없는 백업은
백업이 아니다.*

⚠️ **크기만 보지 않는다.** 잘린 다운로드는 크기로 잡히지만 **바뀐 파일은 크기가 같을
수 있다.** 릴리스 자산에 게시된 다이제스트가 없으므로, 처음 받은 사람이 계산한 값을
여기에 박아 두고 이후에는 그것과 대조한다.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 한 번에 읽는 크기. 34MB 를 통째로 메모리에 올릴 이유가 없다.
CHUNK = 1024 * 1024


def _make_console_survivable() -> None:
    """⚠️ **한국어 Windows 콘솔은 cp949 라 `—` 나 이모지에서 죽는다.**

    실제로 이 도구가 첫 실행에서 `UnicodeEncodeError` 로 죽었다. **검증 도구가
    출력 때문에 죽으면 검증을 못 한다** — 글자가 물음표로 나오는 것보다 나쁘다.
    그래서 인코딩 실패를 치환으로 낮춘다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


@dataclass(frozen=True, slots=True)
class Weight:
    """받아야 할 파일 하나와 **그것이 무엇인지 증명하는 값들**."""

    dest: str
    url: str
    size: int
    sha256: str
    note: str


#: ⚠️ **여기가 정본이다.** `models/README.md` 의 표는 이 값을 사람이 읽게 옮긴 것이다.
WEIGHTS: tuple[Weight, ...] = (
    Weight(
        dest="models/coco.onnx",
        url=(
            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx"
        ),
        size=35_858_002,
        sha256="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        note="YOLOX-S · Apache-2.0 · Megvii Inc. (ADR-24)",
    ),
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(weight: Weight, path: Path) -> str | None:
    """맞으면 `None`, 틀리면 **무엇이 틀렸는지** 돌려준다."""
    if not path.is_file():
        return "파일 없음"
    actual_size = path.stat().st_size
    if actual_size != weight.size:
        return f"크기 불일치: {actual_size} != {weight.size} (잘린 다운로드일 수 있다)"
    actual_hash = sha256_of(path)
    if actual_hash != weight.sha256:
        return f"해시 불일치:\n    실제 {actual_hash}\n    기대 {weight.sha256}"
    return None


def download(weight: Weight, path: Path) -> None:
    """받는다. **부분 파일을 남기지 않는다** — 다음 실행이 그것을 완성품으로 본다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    print(f"  받는 중 … {weight.url}")
    try:
        with (
            urllib.request.urlopen(weight.url, timeout=60) as response,
            partial.open("wb") as handle,
        ):
            done = 0
            while chunk := response.read(CHUNK):
                handle.write(chunk)
                done += len(chunk)
                print(f"\r  {done / weight.size * 100:5.1f}%", end="", flush=True)
        print()
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def run(*, check_only: bool, force: bool) -> int:
    failures = 0
    for weight in WEIGHTS:
        path = ROOT / weight.dest
        print(f"\n{weight.dest}  —  {weight.note}")

        if force and not check_only:
            path.unlink(missing_ok=True)

        problem = verify(weight, path)
        if problem and not check_only:
            print(f"  {problem}")
            download(weight, path)
            problem = verify(weight, path)
            if problem:
                # ⚠️ 검증 실패한 파일을 남기지 않는다. 남기면 다음 실행이 그것을 쓴다.
                path.unlink(missing_ok=True)
                print(f"  ❌ 받은 뒤에도 검증 실패 — 삭제했다\n  {problem}")
                failures += 1
                continue

        if problem:
            print(f"  ❌ {problem}")
            failures += 1
        else:
            print(f"  ✅ 검증됨 ({weight.size:,} 바이트)")

    if failures:
        print(f"\n{failures}개 실패. `models/README.md` 를 확인한다.")
    else:
        print("\n전부 검증됨.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    _make_console_survivable()
    parser = argparse.ArgumentParser(prog="fetch_models", description=__doc__)
    parser.add_argument("--check", action="store_true", help="받지 않고 검증만 한다")
    parser.add_argument("--force", action="store_true", help="있어도 다시 받는다")
    args = parser.parse_args(argv)
    return run(check_only=args.check, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
