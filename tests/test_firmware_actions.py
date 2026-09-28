"""펌웨어의 액션 표와 `config.yaml` 이 어긋나지 않게 잠근다 (WBS 4.1.2 · FR-1.2).

⚠️ **전선으로는 숫자가 오는데 벤더는 이름으로 찾는다.** `ACTION {id}` 를 받은 펌웨어가
`id → 이름` 표를 보고 `action_run(이름)` 을 부르므로, 설정과 펌웨어의 표가 어긋나면
**조용히 다른 동작이 나간다.** 규약과 구현을 골든 픽스처로 묶는 것과 같은 이유다
(PROTOCOL 6절) — 사람의 규칙 준수에 의존하지 않는다.

⚠️ **벤더는 모르는 이름에 오류를 내지 않는다.** `act_run_func` 가 두 목록을 훑고 못
찾으면 그냥 끝난다. 그래서 오타 하나가 **"조용한 무동작"** 이 되고, 실기에서 *"명령은
갔는데 로봇이 안 움직인다"* 로만 보인다. 이 시험이 그 자리를 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.yaml"
HAL = ROOT / "firmware_mechdog_motion" / "src" / "motion_hal.cpp"

#: 벤더가 구간마다 `delay()` 로 블로킹하므로 여러 구간짜리는 싣지 않는다.
#: 1,000ms 단일 구간이라도 그동안 `loop()` 가 멈춘다 — 정지 상태에서만 받는다.
MAX_BLOCKING_MS = 1000


def firmware_table() -> dict[int, str]:
    """`kActionNames[]` 를 읽는다. 컴파일 없이 소스만 본다."""
    source = HAL.read_text(encoding="utf-8")
    body = re.search(r"const ActionName kActionNames\[\]\s*=\s*\{(.*?)\};", source, re.S)
    assert body is not None, "kActionNames 표를 찾지 못했다 — 이름이 바뀌었으면 이 시험도 고친다"
    return {
        int(num): name
        for num, name in re.findall(r'\{\s*(\d+)\s*,\s*"([^"]+)"\s*\}', body.group(1))
    }


def config_table() -> dict[int, str]:
    section = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["actions"]["id_map"]
    return {int(key): str(value) for key, value in section.items()}


def test_firmware_and_config_agree_on_every_action() -> None:
    """⚠️ **어긋나면 다른 동작이 나간다.** 숫자만 맞고 이름이 다르면 아무도 모른다."""
    assert firmware_table() == config_table()


def test_ids_fit_the_protocol_range() -> None:
    """규약이 `ACTION.id` 를 0~15 로 정해 두었다 (PROTOCOL 2절)."""
    for action_id in config_table():
        assert 0 <= action_id <= 15, f"ACTION id {action_id} 가 규약 범위 밖"


def test_only_single_segment_actions_are_mapped() -> None:
    """⚠️ **여러 구간짜리는 싣지 않는다** — 블로킹이 길어져 `loop()` 가 그만큼 멈춘다.

    벤더 `action.h` 는 저장소에 없으므로(ADR-20) 여기서는 **표에 실린 이름이 우리가
    확인한 단일 구간 셋인지**만 본다. 실제 구간 수 확인은 실기에서 한다 — 벤더 파일을
    읽는 시험을 만들면 CI 가 그 파일을 요구하게 되고, 그것은 넣을 수 없다.
    """
    verified_single_segment = {"stand_four_legs", "sit_dowm", "go_prone"}
    unexpected = set(config_table().values()) - verified_single_segment
    assert not unexpected, (
        f"구간 수를 확인하지 않은 액션이 실렸다: {sorted(unexpected)} — "
        f"단일 구간({MAX_BLOCKING_MS}ms)인지 벤더 `action.h` 에서 확인하고 이 목록에 넣는다"
    )


def test_the_vendor_typo_is_preserved() -> None:
    """⚠️ `sit_dowm` 은 벤더 오타다. **고쳐 적으면 벤더가 못 찾고 아무 일도 안 한다.**"""
    assert config_table()[1] == "sit_dowm", "벤더 철자를 그대로 둔다"
