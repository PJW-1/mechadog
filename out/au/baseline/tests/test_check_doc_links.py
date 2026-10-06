"""문서 상대 링크 검사기의 판정 로직.

문서를 옮기거나 링크를 고칠 때 대상이 실제로 있는지 사람이 매번 눈으로
확인하는 대신, 정상 링크·깨진 링크·외부 링크·코드 블록 속 링크를 각각
구성해 판정이 맞는지 본다.
"""

from __future__ import annotations

from pathlib import Path

from tools.dev.check_doc_links import find_broken_links, main, tracked_markdown_files


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_link_to_existing_file_is_not_broken(tmp_path: Path) -> None:
    _write(tmp_path / "target.md", "# target")
    doc = _write(tmp_path / "doc.md", "[대상](target.md) 참조.")
    assert find_broken_links(doc) == []


def test_link_to_missing_file_is_broken(tmp_path: Path) -> None:
    doc = _write(tmp_path / "doc.md", "[없음](missing.md) 참조.")
    broken = find_broken_links(doc)
    assert broken == [(1, "missing.md")]


def test_link_with_anchor_checks_only_the_file(tmp_path: Path) -> None:
    _write(tmp_path / "target.md", "# target\n## adr-1")
    doc = _write(tmp_path / "doc.md", "[대상](target.md#adr-1) 참조.")
    # 앵커 존재는 보지 않는다 — 없는 앵커라도 파일만 있으면 통과한다.
    doc2 = _write(tmp_path / "doc2.md", "[대상](target.md#no-such-anchor) 참조.")
    assert find_broken_links(doc) == []
    assert find_broken_links(doc2) == []


def test_link_to_directory_is_not_broken(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    doc = _write(tmp_path / "doc.md", "[폴더](sub/) 참조.")
    assert find_broken_links(doc) == []


def test_relative_parent_link_is_resolved_from_doc_dir(tmp_path: Path) -> None:
    _write(tmp_path / "ARCHITECTURE.md", "# arch")
    doc = _write(tmp_path / "internal" / "doc.md", "[상위](../ARCHITECTURE.md) 참조.")
    assert find_broken_links(doc) == []


def test_http_and_mailto_links_are_skipped(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "doc.md",
        "[웹](https://example.com/missing.md)\n"
        "[메일](mailto:a@example.com)\n"
        "[순수 앵커](#section)\n",
    )
    assert find_broken_links(doc) == []


def test_links_inside_fenced_code_block_are_skipped(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "doc.md",
        "\n".join(
            [
                "설명",
                "```",
                "[없음](missing.md)",
                "```",
                "[역시없음](also-missing.md)",
            ]
        ),
    )
    broken = find_broken_links(doc)
    assert broken == [(5, "also-missing.md")]


def test_tracked_markdown_files_includes_known_repo_doc() -> None:
    files = tracked_markdown_files()
    assert any(f.name == "README.md" for f in files)


def test_main_returns_zero_when_repo_docs_have_no_broken_links() -> None:
    assert main([]) == 0


def test_main_reports_failures_and_returns_one(tmp_path: Path, monkeypatch, capsys) -> None:
    doc = _write(tmp_path / "doc.md", "[없음](missing.md)")
    monkeypatch.setattr("tools.dev.check_doc_links.tracked_markdown_files", lambda: [doc])
    monkeypatch.setattr("tools.dev.check_doc_links.ROOT", tmp_path)

    assert main([]) == 1
    out = capsys.readouterr().out
    assert "missing.md" in out
