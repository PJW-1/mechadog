"""담당자별 작업 목록과 WBS 작업 사전의 일치 검증.

`docs/ASSIGNMENTS.md`는 `docs/WBS.md` 작업 사전에서 **생성된 파일**이다. 같은 숫자를
두 곳에 두면 반드시 어긋나므로, 손으로 고치는 것을 막고 재생성 결과와 대조한다.

이 프로젝트에서 실제로 여러 번 어긋났다 — 총 공수 69.0 vs 70.0, WBS `5.0` 절 제목
5.5 vs 하위 합 6.5, 명령 7종 vs 8종. 전부 사람이 한쪽만 고쳐서 생긴 것이다.
그래서 **사람의 규칙 준수에 의존하지 않는다** (CONTRIBUTING 5절).
"""

import re

import pytest
from conftest import ROOT

from tools.wbs_assignments import OUT, WorkPackage, is_ready, main, parse_wbs, render


@pytest.fixture(scope="module")
def packages() -> list[WorkPackage]:
    return parse_wbs()


def test_generated_file_matches_wbs() -> None:
    """**WBS 를 고치고 재생성하지 않으면 여기서 실패한다.**

    실패했다면 `python tools/wbs_assignments.py` 를 실행하고 결과를 커밋한다.
    """
    assert main(["--check"]) == 0, "docs/ASSIGNMENTS.md 를 재생성하고 커밋하라"


def test_total_effort_matches_the_wbs_header(packages: list[WorkPackage]) -> None:
    """워크패키지 합이 WBS 헤더의 총 계획 공수와 같아야 한다."""
    body = (ROOT / "docs" / "WBS.md").read_text(encoding="utf-8")
    line = next(x for x in body.splitlines() if "총 계획 공수" in x)
    stated = float(line.split("**")[3].split()[0])
    assert sum(p.effort for p in packages) == pytest.approx(stated)


def test_every_package_has_exactly_one_owner(packages: list[WorkPackage]) -> None:
    """배정되지 않은 워크패키지가 없어야 한다 — 그것이 곧 아무도 안 하는 일이다."""
    assert packages, "WBS 작업 사전 파싱 결과가 비어 있다"
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


def test_completed_predecessor_unlocks_work(packages: list[WorkPackage]) -> None:
    """선행 칸이 비어 있을 때만 준비로 보던 회귀를 막는다."""
    by_id = {p.wid: p for p in packages}
    assert by_id["4.1.3"].done, "근거가 붙은 완료 표기를 인식하지 못한다"
    assert is_ready(by_id["3.4.1"], packages), "완료된 3.1.1이 FSM 착수를 막고 있다"
    assert is_ready(by_id["4.1.3"], packages), "완료된 3.1.1·3.1.2가 C++ 파서를 막고 있다"
    assert is_ready(by_id["4.3.2"], packages), "완료된 4.3.1이 UDP commander를 막고 있다"


def test_group_and_range_predecessors_stay_blocked(packages: list[WorkPackage]) -> None:
    """묶음 선행은 그 안의 작업이 모두 끝나야 풀린다."""
    by_id = {p.wid: p for p in packages}
    assert not is_ready(by_id["2.5"], packages)
    # WBS 밖 조건(장비 도착·승인)은 맞는 ID 가 없으므로 계속 대기다.
    assert not is_ready(by_id["2.2.1"], packages)


def test_dot_separated_predecessors_unlock_together(packages: list[WorkPackage]) -> None:
    """가운뎃점으로 묶인 선행도 쉼표와 똑같이 읽어야 한다.

    `3.2.5` 의 선행은 `3.2.1·3.2.2·3.2.4, 3.2.6` 이다. 쉼표로만 나누면 첫 덩어리가
    어떤 ID 와도 맞지 않아 **선행이 전부 끝나도 영원히 대기로 남았다** — 2026-09-17
    에 `3.2.2`·`3.2.6` 을 닫고도 다음 작업이 목록에 뜨지 않아 드러났다.
    """
    by_id = {p.wid: p for p in packages}
    blocker = by_id["3.2.5"]
    assert "·" in blocker.predecessor
    parts = [by_id[wid] for wid in ("3.2.1", "3.2.2", "3.2.4", "3.2.6")]
    assert is_ready(blocker, packages) == all(part.done for part in parts)


def _sections(text: str, heading: str) -> str:
    """담당자마다 `heading` 으로 시작하는 절을 다음 `###`·`<details>` 전까지 모아 돌려준다."""
    parts = []
    start = text.find(heading)
    while start != -1:
        ends = [
            i for i in (text.find("\n### ", start + 1), text.find("<details>", start)) if i != -1
        ]
        parts.append(text[start : min(ends)])
        start = text.find(heading, start + 1)
    return "".join(parts)


def test_approved_phase2_packages_are_listed_as_work(
    packages: list[WorkPackage],
) -> None:
    """Phase 2 착수를 승인했으므로(2026-09-28 · WBS `1.4`) `[P2]` 항목도 할 일이다.

    승인 전에는 `⏸` 절에 따로 뒀다. 승인 뒤에도 그대로 두면 담당자가 잡을 수 있는
    일이 목록에서 빠지고, 선행에 «P2 승인» 이 남으면 WBS 번호가 아니라서
    **영원히 대기로 남는다** — `5.4.1` 이 그랬다.
    """
    text = render(packages)
    work = _sections(text, "### 🟢") + _sections(text, "### ⏳")
    assert "### ⏸" not in text
    by_id = {p.wid: p for p in packages}
    # 승인 때 선행을 고친 세 행과 새로 등재한 `3.9.0` 은 표기가 빠져도 알아채도록 못 박는다.
    for wid in (
        "2.2.1",
        "2.5",
        "3.6.1",
        "3.6.5",
        "3.9.0",
        "3.9.1",
        "3.9.2",
        "5.4.1",
        "5.4.2",
        "5.4.5",
    ):
        assert by_id[wid].phase2, f"{wid}: [P2] 표기 누락"
    for package in packages:
        if not package.phase2:
            continue
        assert "P2 승인" not in package.predecessor, f"{package.wid}: 승인된 조건이 선행에 남았다"
        if not package.done:
            assert f"`{package.wid}`" in work, f"{package.wid}: 할 일 목록에 없다"


def test_section_headings_match_their_packages(packages: list[WorkPackage]) -> None:
    """절 제목의 공수가 그 아래 워크패키지 합과 같아야 한다.

    `3.9.0` 을 등재하며 `#### 3.9` 와 총 공수는 고쳤지만 `### 3.0` 제목은 26.0 으로
    남았다(2026-09-28 · Devin 검수). 총 공수 시험은 행 합만 보므로 절 제목은 못 잡았다.
    """
    body = (ROOT / "docs" / "WBS.md").read_text(encoding="utf-8")
    headings = re.findall(r"^#{3,4} (\d+\.\d+) .*? — ([\d.]+) M/D", body, re.M)
    assert headings, "절 제목을 하나도 읽지 못했다"
    for section, stated in headings:
        major, minor = section.split(".")
        if minor == "0":
            actual = sum(p.effort for p in packages if p.group == major)
        else:
            actual = sum(p.effort for p in packages if f"{p.wid}.".startswith(f"{section}."))
        assert actual == pytest.approx(float(stated)), (
            f"{section}: 제목 {stated} ≠ 하위 합 {actual}"
        )


def test_packages_without_effort_still_appear(packages: list[WorkPackage]) -> None:
    """공수가 `—` 로 비어 있어도 할 일은 목록에 보여야 한다.

    `3.6.4`·`3.6.5`·`4.8.4` 는 공수 산정 전에 추가돼 파서가 건너뛰었고,
    진행 중인 일이 담당 목록에서 통째로 사라져 있었다.
    """
    text = render(packages)
    for wid in ("3.6.4", "3.6.5", "4.8.4"):
        assert f"`{wid}`" in text, f"{wid}: 담당 목록에 없다"
