"""담당자별 작업 목록의 WBS 일치 검증 (WBS 7.1).

`docs/ASSIGNMENTS.md` 는 `docs/WBS.md` 3절에서 **생성된 파일**이다. 같은 숫자를
두 곳에 두면 반드시 어긋나므로, 손으로 고치는 것을 막고 재생성 결과와 대조한다.

이 프로젝트에서 실제로 여러 번 어긋났다 — 총 공수 69.0 vs 70.0, WBS `5.0` 절 제목
5.5 vs 하위 합 6.5, 명령 7종 vs 8종. 전부 사람이 한쪽만 고쳐서 생긴 것이다.
그래서 **사람의 규칙 준수에 의존하지 않는다** (CONTRIBUTING 5절).
"""

import re

import pytest
from conftest import ROOT

from tools.wbs_assignments import OUT, WorkPackage, main, parse_wbs, render


@pytest.fixture(scope="module")
def packages() -> list[WorkPackage]:
    return parse_wbs()


def test_generated_file_matches_wbs() -> None:
    """**WBS 를 고치고 재생성하지 않으면 여기서 실패한다.**

    실패했다면 `python tools/wbs_assignments.py` 를 실행하고 결과를 커밋한다.
    """
    assert main(["--check"]) == 0, "docs/ASSIGNMENTS.md 를 재생성하고 커밋하라"


def test_total_effort_matches_the_wbs_header(packages: list[WorkPackage]) -> None:
    """워크패키지 합이 WBS 헤더의 총 계획 공수와 같아야 한다 (0절 100% 규칙)."""
    body = (ROOT / "docs" / "WBS.md").read_text(encoding="utf-8")
    line = next(x for x in body.splitlines() if "총 계획 공수" in x)
    stated = float(line.split("**")[3].split()[0])
    assert sum(p.effort for p in packages) == pytest.approx(stated)


def test_owner_totals_match_the_wbs_rollup(packages: list[WorkPackage]) -> None:
    """담당자별 합이 WBS 4절 담당자 기준 표와 같아야 한다.

    여기가 어긋나면 **누군가의 부하가 실제와 다르게 계획되어 있다**는 뜻이다.
    """
    body = (ROOT / "docs" / "WBS.md").read_text(encoding="utf-8")
    # 다음 소제목까지를 구간으로 잡는다. 특정 제목 문자열에 의존하면 그 제목이
    # 바뀔 때 테스트가 깨진다 — 실제로 한 번 깨졌다.
    match = re.search(r"^### 담당자 기준$(.*?)(?=^### )", body, re.S | re.M)
    assert match, "WBS 4절에 '담당자 기준' 절이 없다"
    section = match.group(1)
    for owner, marker in (("L1·L2", "L1 · L2"), ("S", "S · 팀장")):
        row = next(x for x in section.splitlines() if marker in x)
        stated = float(row.rsplit("**", 2)[1])
        actual = sum(p.effort for p in packages if p.owner == owner)
        assert actual == pytest.approx(stated), f"{owner}: 표기 {stated} / 실제 {actual}"


def test_every_package_has_exactly_one_owner(packages: list[WorkPackage]) -> None:
    """배정되지 않은 워크패키지가 없어야 한다 — 그것이 곧 아무도 안 하는 일이다."""
    assert packages, "WBS 3절 파싱 결과가 비어 있다"
    for p in packages:
        assert p.owner in ("L1·L2", "S"), f"{p.wid}: 담당 미배정 (R={p.role!r})"


def test_work_package_ids_are_unique(packages: list[WorkPackage]) -> None:
    """같은 번호가 두 번 나오면 공수가 이중 계상된다."""
    ids = [p.wid for p in packages]
    assert len(ids) == len(set(ids)), "중복된 WBS 번호가 있다"


def test_generated_file_warns_against_hand_editing() -> None:
    """생성 파일임을 읽는 사람이 알아야 한다. 없으면 누가 직접 고친다."""
    text = OUT.read_text(encoding="utf-8")
    assert "이 파일은 생성된다" in text
    assert "tools/wbs_assignments.py" in text


def test_render_is_deterministic(packages: list[WorkPackage]) -> None:
    """같은 입력에 같은 출력이어야 `--check` 대조가 성립한다."""
    assert render(packages) == render(parse_wbs())
