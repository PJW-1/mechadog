"""가중치 검증 로직 (WBS 3.3.1).

**받는 것은 시험하지 않는다** — 네트워크에 의존하는 시험은 CI 를 불안정하게 만든다.
대신 **무엇을 틀렸다고 판정하는지**를 고정한다. 그게 이 도구의 존재 이유다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tools.fetch_models import WEIGHTS, Weight, sha256_of, verify


def _weight(payload: bytes, *, sha: str | None = None) -> Weight:
    return Weight(
        dest="models/x.onnx",
        url="https://example.invalid/x.onnx",
        size=len(payload),
        sha256=sha or hashlib.sha256(payload).hexdigest(),
        note="시험용",
    )


def test_matching_file_passes(tmp_path: Path) -> None:
    payload = b"weights" * 100
    path = tmp_path / "x.onnx"
    path.write_bytes(payload)
    assert verify(_weight(payload), path) is None


def test_missing_file_is_reported(tmp_path: Path) -> None:
    assert verify(_weight(b"abc"), tmp_path / "없음.onnx") == "파일 없음"


def test_truncated_download_is_caught_by_size(tmp_path: Path) -> None:
    """잘린 다운로드는 크기로 잡힌다 — 해시를 계산하기 전에 걸러 값싸게 끝낸다."""
    payload = b"weights" * 100
    path = tmp_path / "x.onnx"
    path.write_bytes(payload[:-10])
    problem = verify(_weight(payload), path)
    assert problem is not None and "크기 불일치" in problem


def test_same_size_different_content_is_caught_by_hash(tmp_path: Path) -> None:
    """⚠️ **크기만 보면 안 된다.** 바뀐 파일은 크기가 같을 수 있다."""
    payload = b"A" * 700
    swapped = b"B" * 700
    path = tmp_path / "x.onnx"
    path.write_bytes(swapped)
    problem = verify(_weight(payload), path)
    assert problem is not None and "해시 불일치" in problem


def test_hash_is_streamed_not_loaded_whole(tmp_path: Path) -> None:
    """34MB 를 통째로 메모리에 올리지 않는다 — 청크 경계에서도 값이 같아야 한다."""
    payload = bytes(range(256)) * 8192  # 2MiB, 청크(1MiB) 두 개보다 크다
    path = tmp_path / "x.onnx"
    path.write_bytes(payload)
    assert sha256_of(path) == hashlib.sha256(payload).hexdigest()


# ── 정본 검사 ───────────────────────────────────────────────
def test_manifest_is_filled_in() -> None:
    """⚠️ **해시 자리를 비워 두면 검증이 없는 것과 같다.**

    릴리스 자산에 게시된 다이제스트가 없어서 우리가 계산해 박아 넣는 값이다.
    """
    assert WEIGHTS, "받을 파일이 하나는 있어야 한다"
    for weight in WEIGHTS:
        assert len(weight.sha256) == 64, f"{weight.dest}: SHA-256 이 64자여야 함"
        assert set(weight.sha256) <= set("0123456789abcdef"), "소문자 16진수"
        assert weight.size > 0
        assert weight.url.startswith("https://"), "평문 HTTP 로 받지 않는다"
        assert weight.note, "출처와 라이선스를 적는다"


def test_readme_quotes_the_same_numbers() -> None:
    """⚠️ **정본은 스크립트다.** 문서의 숫자가 어긋나면 사람이 잘못된 값을 대조한다."""
    readme = (Path(__file__).resolve().parents[1] / "models" / "README.md").read_text(
        encoding="utf-8"
    )
    for weight in WEIGHTS:
        assert weight.sha256 in readme, f"{weight.dest}: README 의 해시가 다르다"
        assert f"{weight.size:,}" in readme or str(weight.size) in readme
