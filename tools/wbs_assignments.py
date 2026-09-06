"""담당자별 작업 목록 생성기 (WBS 7.1).

`docs/WBS.md` 3절 사전을 파싱해 **누가 무엇을 하는가**를 `docs/ASSIGNMENTS.md` 로
펼친다. WBS 는 산출물 기준으로 분해되어 있어서(0절 작성 규칙), 담당자 한 사람의
할 일이 7개 대분류에 흩어진다. `R` 열을 눈으로 훑어야 자기 일을 알 수 있다.

**왜 손으로 쓰지 않는가** — 같은 숫자를 두 곳에 두면 반드시 어긋난다. 이 프로젝트에서
이미 여러 번 일어났다(총 공수 69.0 vs 70.0, 절 제목 5.5 vs 하위 합 6.5, 명령 7종 vs 8종).
그래서 **WBS 를 정본으로 두고 생성**하며, `tests/test_assignments.py` 가 커밋된 파일과
재생성 결과를 대조한다. WBS 를 고치고 재생성하지 않으면 CI 가 실패한다.

    python tools/wbs_assignments.py            # 생성 (파일 갱신)
    python tools/wbs_assignments.py --check    # 대조만 (CI·테스트용)
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WBS = ROOT / "docs" / "WBS.md"
OUT = ROOT / "docs" / "ASSIGNMENTS.md"

#: WBS 3절 사전의 행 구조 — 8칸 고정.
#: `ID | 워크패키지 | 산출물 | 완료 기준(DoD) | R | 선행 | M/D | 연계`
_COLUMNS = 8
_ID, _NAME, _DELIV, _DOD, _R, _PRED, _MD = 0, 1, 2, 3, 4, 5, 6

#: 완료 표기 — WBS 사전의 DoD 칸 끝에 붙인다. 진척은 여기 한 곳에서만 관리한다.
DONE_MARK = "[완료]"

#: 담당자 배정 규칙 — 정본은 [WBS 4절 담당자 기준]이다.
#: `R=A` 는 전부 L1·L2 이고, 여기에 `3.9`(구역 순찰)·`5.4.1`(ROS2 컨테이너)이 이관된다.
#: 나머지 `B`·`C` 가 팀장 몫이다.
TRANSFERRED_TO_L: tuple[str, ...] = ("3.9", "5.4.1")

OWNERS: dict[str, str] = {
    "L1·L2": "펌웨어 · 하드웨어 · 온보드 안전로직 · 펌웨어 CI · 공간·항법",
    "S": "비전 · PPE · 변화감지 · 인증 · FSM · 통신 · config·로깅 · 대시보드 · 시험 · 문서",
}

#: 대분류 제목 — 소계 표에 쓴다.
GROUPS: dict[str, str] = {
    "1": "요구사항 및 범위 정의",
    "2": "장치 및 하드웨어",
    "3": "판단 로직 및 핵심 기능",
    "4": "소프트웨어 및 화면",
    "5": "통합 및 형상관리",
    "6": "시험 및 품질",
    "7": "보고 및 최종 제출",
}


@dataclass(frozen=True, slots=True)
class WorkPackage:
    wid: str
    name: str
    deliverable: str  # 무엇을 만들면 되는가 — 초보가 가장 먼저 찾는 정보
    role: str  # A · B · C (작업 성격)
    predecessor: str
    effort: float
    done: bool

    @property
    def group(self) -> str:
        return self.wid.split(".")[0]

    @property
    def owner(self) -> str:
        if any(self.wid.startswith(p) for p in TRANSFERRED_TO_L):
            return "L1·L2"
        return "L1·L2" if self.role == "A" else "S"

    def sort_key(self) -> tuple[int, ...]:
        return tuple(int(p) for p in self.wid.split("."))


def _clean(cell: str) -> str:
    return cell.replace("**", "").strip()


def parse_wbs(path: Path = WBS) -> list[WorkPackage]:
    """3절 사전에서 워크패키지를 뽑는다. 2절 트리·4절 롤업은 대상이 아니다."""
    body = path.read_text(encoding="utf-8")
    start = body.index("## 3. WBS 사전")
    end = body.index("## 4. 공수 롤업")

    packages: list[WorkPackage] = []
    for line in body[start:end].splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != _COLUMNS:
            continue
        wid = _clean(cells[_ID])
        if not re.fullmatch(r"\d+(\.\d+)*", wid):
            continue
        effort = _clean(cells[_MD])
        if not re.fullmatch(r"[\d.]+", effort):
            continue
        packages.append(
            WorkPackage(
                wid=wid,
                name=_clean(cells[_NAME]),
                deliverable=_clean(cells[_DELIV]) or "—",
                role=_clean(cells[_R]),
                predecessor=_clean(cells[_PRED]) or "—",
                effort=float(effort),
                done=DONE_MARK in cells[_DOD],
            )
        )
    return sorted(packages, key=WorkPackage.sort_key)


def _dependency_ids(token: str, packages: list[WorkPackage]) -> set[str]:
    """선행 표기의 한 토큰을 실제 워크패키지 ID로 펼친다.

    `4.3.1` 같은 정확한 ID, `4.1` 같은 묶음, `3.2.1~4`와
    `2.1~2.4` 같은 범위를 지원한다. WBS 밖 조건(예: 장비 도착)은
    매칭되는 ID가 없으므로 완료 전까지 대기로 남는다.
    """
    token = token.strip()
    by_id = {p.wid for p in packages}
    if token in by_id:
        return {token}

    if "~" not in token:
        prefix = f"{token}."
        return {wid for wid in by_id if wid.startswith(prefix)}

    start_text, end_text = (part.strip() for part in token.split("~", 1))
    try:
        start = tuple(int(part) for part in start_text.split("."))
        if "." in end_text:
            end = tuple(int(part) for part in end_text.split("."))
        else:
            end = (*start[:-1], int(end_text))
    except ValueError:
        return set()
    if len(start) != len(end):
        return set()

    matched = set()
    for wid in by_id:
        parts = tuple(int(part) for part in wid.split("."))
        if len(parts) >= len(start) and start <= parts[: len(start)] <= end:
            matched.add(wid)
    return matched


def is_ready(package: WorkPackage, packages: list[WorkPackage]) -> bool:
    """선행 작업이 없거나, 명시된 선행 작업이 모두 완료됐는지 판정한다."""
    if package.predecessor == "—":
        return True

    by_id = {p.wid: p for p in packages}
    for token in package.predecessor.split(","):
        dependencies = _dependency_ids(token, packages)
        if not dependencies or not all(by_id[wid].done for wid in dependencies):
            return False
    return True


def _table(packages: list[WorkPackage], *, with_predecessor: bool) -> list[str]:
    head = (
        "| WBS | 할 일 | 만들 것 | 기다리는 것 | M/D |"
        if with_predecessor
        else ("| WBS | 할 일 | 만들 것 | M/D |")
    )
    rule = (
        "| :--- | :--- | :--- | :--- | ---: |"
        if with_predecessor
        else "| :--- | :--- | :--- | ---: |"
    )
    rows = []
    for p in packages:
        cells = [f"`{p.wid}`", p.name, p.deliverable]
        if with_predecessor:
            cells.append(p.predecessor)
        cells.append(f"{p.effort:.1f}")
        rows.append("| " + " | ".join(cells) + " |")
    return [head, rule, *rows]


def render(packages: list[WorkPackage]) -> str:
    """담당자별 마크다운을 만든다.

    **정렬 기준은 대분류가 아니라 "지금 할 수 있는가" 다.** 처음 보는 사람이 묻는 것은
    *"내 일이 어느 대분류에 속하나"* 가 아니라 *"오늘 뭘 잡으면 되나"* 이기 때문이다.
    대분류 소계는 총량을 보기 위한 것이므로 뒤로 뺀다.
    """
    total = sum(p.effort for p in packages)
    lines: list[str] = [
        "# 내가 할 일",
        "",
        "> ⚠️ **이 파일은 생성된다. 직접 고치지 마라.**",
        "> 정본은 [WBS 3절 사전](WBS.md)이며, 여기는 그것을 담당자 기준으로 펼친 것이다.",
        ">",
        "> ```",
        "> python tools/wbs_assignments.py",
        "> ```",
        ">",
        "> WBS 를 고치고 재생성하지 않으면 `tests/test_assignments.py` 가 CI 에서 실패한다.",
        "",
        "**읽는 법** — 자기 이름을 찾고 🟢 부터 잡는다. 선행 작업이 없거나 모두 끝난 것들이다.",
        "**끝났다고 말할 수 있는 조건(DoD)** 은 [WBS 3절 사전](WBS.md)에서 같은 번호를 찾으면 있다.",
        "",
        "| 담당 | ✅ 완료 | 🟢 지금 가능 | ⏳ 대기 | 남은 공수 | 전체 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    left = 0.0
    for owner in OWNERS:
        mine = [p for p in packages if p.owner == owner]
        todo = [p for p in mine if not p.done]
        ready = [p for p in todo if is_ready(p, packages)]
        remaining = sum(p.effort for p in todo)
        left += remaining
        lines.append(
            f"| **{owner}** | {len(mine) - len(todo)}건 | 🟢 {len(ready)}건 | "
            f"⏳ {len(todo) - len(ready)}건 | **{remaining:.1f}** M/D | "
            f"{sum(p.effort for p in mine):.1f} M/D |"
        )
    lines += [
        f"| | | | | **{left:.1f}** M/D | **{total:.1f}** M/D |",
        "",
        "---",
        "",
    ]

    for owner, scope in OWNERS.items():
        mine = [p for p in packages if p.owner == owner]
        done = [p for p in mine if p.done]
        todo = [p for p in mine if not p.done]
        ready = [p for p in todo if is_ready(p, packages)]
        waiting = [p for p in todo if not is_ready(p, packages)]
        lines += [
            f"## {owner}",
            "",
            f"**담당 영역** — {scope}",
            "",
            f"**남은 공수 {sum(p.effort for p in todo):.1f} M/D · {len(todo)}건** "
            f"(전체 {sum(p.effort for p in mine):.1f} M/D · {len(mine)}건)",
            "",
            f"### 🟢 지금 시작할 수 있다 — {len(ready)}건 · {sum(p.effort for p in ready):.1f} M/D",
            "",
            "선행 작업이 없거나 모두 완료됐다. 이 중 하나를 잡으면 된다.",
            "",
            *_table(ready, with_predecessor=False),
            "",
            f"### ⏳ 선행이 끝나야 시작한다 — {len(waiting)}건 · "
            f"{sum(p.effort for p in waiting):.1f} M/D",
            "",
            "**기다리는 것** 열의 번호가 끝나면 시작할 수 있다.",
            "",
            *_table(waiting, with_predecessor=True),
            "",
            f"<details><summary>✅ 완료 — {len(done)}건 · "
            f"{sum(p.effort for p in done):.1f} M/D</summary>",
            "",
            *_table(done, with_predecessor=False),
            "",
            "</details>",
            "",
            "<details><summary>대분류별 소계</summary>",
            "",
            "| 대분류 | 건수 | 공수 |",
            "| :--- | ---: | ---: |",
        ]
        for gid, gname in GROUPS.items():
            grouped = [p for p in mine if p.group == gid]
            if not grouped:
                continue
            lines.append(
                f"| **{gid}.0** {gname} | {len(grouped)}건 | {sum(p.effort for p in grouped):.1f} |"
            )
        lines += ["", "</details>", "", "---", ""]

    lines += [
        "> **여기 없는 작업은 프로젝트 범위 밖이다** (WBS 0절 100% 규칙).",
        "> 할 일이 새로 생기면 WBS 3절에 워크패키지로 등재한 뒤 이 파일을 재생성한다.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wbs_assignments", description="담당자별 작업 목록 생성")
    parser.add_argument("--check", action="store_true", help="쓰지 않고 커밋본과 대조만 한다")
    args = parser.parse_args(argv)

    rendered = render(parse_wbs())
    if not args.check:
        OUT.write_text(rendered, encoding="utf-8")
        print(f"{OUT.relative_to(ROOT)} 생성")
        return 0

    if not OUT.exists():
        print(f"{OUT.relative_to(ROOT)} 이 없다. `python tools/wbs_assignments.py` 로 생성하라.")
        return 1
    if OUT.read_text(encoding="utf-8") != rendered:
        print(f"{OUT.relative_to(ROOT)} 가 WBS 와 어긋난다. 재생성하라.")
        return 1
    print("일치")
    return 0


if __name__ == "__main__":
    sys.exit(main())
