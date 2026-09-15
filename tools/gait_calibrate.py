"""보행 이동량 실측 도구 (WBS 2.2.3 · FR-6.4).

    python tools/gait_calibrate.py --host <ip> --mode forward --write
    python tools/gait_calibrate.py --host <ip> --mode reverse        # 가정 확인용
    python tools/gait_calibrate.py --host <ip> --mode turn_left --write
    python tools/gait_calibrate.py --host <ip> --mode turn_right     # 좌우 비대칭 확인
    python tools/gait_calibrate.py --host <ip> --mode reverse_turn   # 회피 개선안
    python tools/gait_calibrate.py --host 127.0.0.1 --mode forward --dry-run   # 목업 연습

**네 모드를 재는 이유가 다르다.** `forward` 와 `turn_left` 는 설정에 들어갈 값이고,
`reverse` 와 `turn_right` 는 **코드가 세운 가정을 확인하는 측정**이다 — 회피 시퀀스는
전진 속도로 후진 시간을 계산하고(`actions.avoid_phases`) 선회도 `+turn_angle_deg`
한 방향(**좌회전**)만 쓴다. 즉 *"후진 = 전진"* 과 *"좌 = 우"* 를 가정한다. 4족
트롯에서 그것이 성립할 이유는 없고, 개체 서보 오프셋도 비대칭이다.

⚠️ **`angle` 의 양수는 반시계 = 로봇의 좌회전이다** (`docs/PROTOCOL.md` 부호 규약).
규약에 이 한 줄이 없어서 `teleop` 의 좌우가 뒤집힌 채로 있었고, 펌웨어가 dry-run
이던 동안에는 드러날 수 없었다.

⚠️ **이 도구는 거리를 재지 못한다.** 로봇에 오도메트리가 없고 텔레메트리 송신도
아직 없다(`4.1.4`). 거리·각도는 **사람이 줄자와 각도기로** 재서 입력한다. 도구가
하는 일은 넷이다.

  ① 정해진 시간만 정확히 구동한다 — 10Hz 송신 창을 열고 닫는다
  ② **실제 송신 창 길이를 기록한다** (요청과 다를 수 있다)
  ③ 3회 이상 반복해 평균·표준편차를 낸다
  ④ `--write` 로 개체 프로파일에 `measured_on` 과 함께 적는다

⚠️ **왜 스톱워치로 재면 안 되는가.** 로봇은 명령이 300ms 끊기면 스스로 멈추므로
(`safety.cmd_timeout_ms`) **실제 구동 시간은 송신 창의 길이 + 최대 한 주기**다.
사람이 *"1초"* 라고 생각한 구간과 로봇이 실제로 걸은 구간은 다르고, 그 차이가
그대로 mm/s 오차가 된다. 그래서 창을 도구가 열고 닫으며 그 길이를 남긴다.

⚠️ **피치·롤 진폭(2.2.3 ③)은 이 도구로 못 낸다.** 펌웨어 텔레메트리 송신(`4.1.4`)과
IMU 드라이버(`OI-23`)가 선행이다. 자리만 만들어 두면 값이 0 으로 채워져 *"쟀다"*
로 보이므로 **아예 거부한다** — 없는 것은 없는 대로 둔다.

⚠️ **시연할 바닥에서 재야 한다.** 카펫과 장판에서 값이 다르다. 표준편차가 크게
나오면 그것이 바닥이 미끄럽다는 신호이며, 그 바닥에서 회피 시퀀스를 신뢰할 수
없다는 뜻이다.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host.behavior.commander import Commander  # noqa: E402
from host.common.config import load_config  # noqa: E402
from host.common.console import survive_encoding_errors  # noqa: E402
from host.common.protocol import CommandEncoder, system_clock_ms  # noqa: E402

#: 모드 → 개체 프로파일 키. **둘만 있다.**
#:
#: ⚠️ **후진과 우선회는 적을 곳이 없다.** 설정에는 `forward_mm_per_sec` 과
#: `turn_deg_per_sec` 뿐인데, 회피 시퀀스는 **전진 속도로 후진 시간을 계산하고**
#: (`actions.avoid_phases`) 선회도 `+turn_angle_deg`(**좌회전**) 한 방향만 쓴다.
#: 즉 *"후진 = 전진"* 과 *"좌 = 우"* 를 가정하고 있다. 그 두 가정을 재 보는 것이
#: 나머지 두 모드의 목적이므로 **자동으로 적지 않고 사람이 비교해 판단하게 한다.**
PROFILE_KEYS: dict[str, str] = {
    "forward": "forward_mm_per_sec",
    # ⚠️ **좌선회다.** `angle` 의 양수는 반시계 = 로봇의 좌회전이고(PROTOCOL 부호
    # 규약), 회피 시퀀스는 `+turn_angle_deg` 를 그대로 쓴다 — 즉 **회피는 좌회전**
    # 하므로 설정에 들어갈 값도 좌선회 실측값이다. 처음에는 우선회를 적게 만들었고,
    # 실물로 몰아 보니 `turn_right` 가 반시계로 돌았다.
    "turn_left": "turn_deg_per_sec",
    # ⚠️ **2026-09-11 실측이 키를 만들었다.** 처음에는 후진·후진 선회에 적을 곳이
    # 없었다 — 회피 시퀀스가 *"후진 = 전진"* 을 가정했기 때문이다. 실측에서 후진이
    # 25% 느리고 전진 선회가 여유를 다 먹는 것이 드러나 세 키가 생겼고
    # ([ADR-29](../docs/DECISIONS.md)), 이제 **도구가 직접 적을 수 있다.**
    # 손으로 YAML 을 고치면 `measured_on` 이 빠지거나 들여쓰기가 깨진다.
    "reverse": "reverse_mm_per_sec",
    "reverse_turn": "reverse_turn_deg_per_sec",
}

#: 모드 → 부 측정을 적을 키. **부 측정이 설정에 들어가는 것은 후진 선회뿐이다.**
#:
#: ⚠️ 회피는 **각도와 여유 두 목표 중 느린 쪽**으로 시간을 잡으므로
#: (`actions.avoid_phases`) 각도만 적으면 여유 목표를 계산할 수 없다. 그래서
#: 후진 선회는 **두 키가 다 있어야** 새 구간표가 켜진다 — 하나만 적히면 개체는
#: 조용히 옛 4구간표(순 여유 음수)로 돌아간다.
SECONDARY_KEYS: dict[str, str] = {"reverse_turn": "reverse_turn_mm_per_sec"}

#: 모드 → (주 측정 이름, 주 단위, 부 측정 이름, 부 단위).
#:
#: **주 측정이 비율이 되고 부 측정은 기록·검산용이다.** 선회의 부 측정이 선택이
#: 아닌 이유 — 제자리 회전이 불가하므로(ADR-11) 선회 중 **앞으로 간다**. 회피는
#: 후진 200mm 로 물러난 뒤 선회하는데, 그 선회가 앞으로 150mm 를 되돌려 주면
#: **순 여유가 50mm 밖에 남지 않고 같은 장애물에 다시 붙는다.** 그 산수를 하려면
#: 선회 중 이동량이 있어야 한다.
#: ⚠️ **거리는 전부 "시작점 → 끝점 직선거리(현)" 다.**
#:
#: 작은 각도에서는 현·정면 성분·호 길이가 거의 같아 표기가 없어도 문제가 없었다.
#: 93.5도를 돌린 측정에서 셋이 **32% 까지 갈렸고** 그때 혼동이 드러났다. 현으로
#: 적으면 각도와 함께 나머지로 변환된다 — `정면 성분 = 현 x cos(각/2)`,
#: `반경 = 현 / (2 sin(각/2))`. 호 길이는 줄자로 재기 어렵고 쓸 데도 없다.
MEASURES: dict[str, tuple[str, str, str, str]] = {
    "forward": ("직선거리", "mm", "방향 변화", "도"),
    "reverse": ("직선거리", "mm", "방향 변화", "도"),
    "turn_right": ("방향 변화", "도", "직선거리", "mm"),
    "turn_left": ("방향 변화", "도", "직선거리", "mm"),
    # 회피의 답. **후진과 선회를 동시에** 하면 선회가 여유를 깎지 않는다.
    "reverse_turn": ("방향 변화", "도", "후퇴 직선거리", "mm"),
}

#: 구동 전 정지 시간. 온보드가 자세를 가라앉힐 시간을 준다 (회피 시퀀스의 `settle` 과 같은 이유).
SETTLE_S = 1.0
#: 표준편차가 평균의 이 비율을 넘으면 경고한다. 바닥이 일정하지 않다는 신호다.
SPREAD_WARN = 0.15


@dataclass(slots=True)
class Acks:
    """로봇이 돌려준 응답 집계. **텔레메트리가 없는 지금 유일한 확인 수단이다.**

    펌웨어는 명령마다 `{"verdict":..,"applied":..,"safe_latched":..}` 를 보낸
    곳으로 되돌려 준다(`sendAck`). 주기 텔레메트리(`4.1.4`)와는 별개다.
    """

    total: int = 0
    applied: int = 0
    latched: int = 0
    discarded: int = 0

    def note(self, row: dict) -> None:
        self.total += 1
        if row.get("applied"):
            self.applied += 1
        if row.get("safe_latched"):
            self.latched += 1
        if str(row.get("verdict", "")).upper() != "ACCEPT":
            self.discarded += 1

    def describe(self) -> str:
        if self.total == 0:
            return "응답 없음 (목업이거나 ACK 미지원)"
        return (
            f"응답 {self.total} · 적용 {self.applied} · 래치 {self.latched} · 폐기 {self.discarded}"
        )


@dataclass(frozen=True, slots=True)
class Trial:
    """한 번의 구동. **요청 시간이 아니라 실제 송신 창을 들고 있다.**"""

    index: int
    requested_s: float
    actual_s: float
    packets: int
    measured: float  # 주 측정 — 사람이 잰 값
    #: 부 측정. 없으면 `None` — 안 잰 것을 0 으로 채우면 "쟀다" 가 된다.
    secondary: float | None = None

    @property
    def rate(self) -> float:
        """단위 시간당 주 측정값. **실제 송신 창으로 나눈다.**"""
        return self.measured / self.actual_s

    @property
    def secondary_rate(self) -> float | None:
        """단위 시간당 부 측정값. 안 쟀으면 `None`."""
        return None if self.secondary is None else self.secondary / self.actual_s


def drain_acks(sock: socket.socket, acks: Acks) -> None:
    """도착한 ACK 를 **막지 않고** 전부 비운다.

    ⚠️ Windows 에서 상대가 없으면 ICMP Port Unreachable 이 `ConnectionResetError`
    로 돌아온다 — UDP 에 연결이 없으므로 의미 없는 오류다. 측정이 그것 때문에
    죽으면 안 된다.
    """
    while True:
        try:
            data, _addr = sock.recvfrom(2048)
        except (BlockingIOError, ConnectionResetError):
            return
        except OSError:
            return
        try:
            acks.note(json.loads(data))
        except (ValueError, TypeError):
            acks.total += 1  # 읽을 수 없는 응답도 온 것은 온 것이다


def pump(
    sock: socket.socket,
    peer: tuple[str, int],
    commander: Commander,
    seconds: float,
    acks: Acks,
) -> tuple[float, int]:
    """`seconds` 동안 **지금 의도를 10Hz 로 계속 보낸다.** 창 길이와 패킷 수를 준다.

    ⚠️ **침묵하면 로봇이 잠긴다.** 명령이 `safety.cmd_timeout_ms`(300ms) 끊기면
    로봇은 스스로 페일세이프로 들어가 래치를 걸고, 그 뒤의 `MOVE` 를 전부 무시한다.
    그래서 *"가만히 있는 구간"* 도 **정지를 계속 보내는 구간**이어야 한다 — 회피
    시퀀스의 `Phase("settle", 0, 0, ...)` 가 바로 그것이다.

    실제로 처음에는 정지 구간을 `time.sleep` 으로 두었고, **로봇이 한 발도 움직이지
    않았다.** ACK 는 `ACCEPT` 로 오는데 `applied` 가 `false` 였다.
    """
    packets = 0
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        now = system_clock_ms()
        for line in commander.tick(now):
            sock.sendto(line.encode("utf-8"), peer)
            packets += 1
        drain_acks(sock, acks)
        # ⚠️ `next_due_ms` 는 프로퍼티다 — 호출하면 `TypeError` 가 난다.
        # 다음 마감까지만 잔다. 자체 주기를 세면 시계가 둘이 된다.
        due = commander.next_due_ms
        if due is not None:
            time.sleep(max(0.0, (due - system_clock_ms()) / 1000))
    drain_acks(sock, acks)
    return time.perf_counter() - started, packets


def drive_window(
    sock: socket.socket,
    peer: tuple[str, int],
    commander: Commander,
    *,
    step_mm: float,
    angle_deg: float,
    seconds: float,
    settle_s: float = 0.0,
) -> tuple[float, int, Acks]:
    """정지로 가라앉힌 뒤 `seconds` 동안 구동한다. **측정 구간만 돌려준다.**

    ⚠️ **래치를 풀고 곧바로 이어서 보낸다.** 해제와 구동 사이에 침묵이 끼면 그
    사이에 다시 잠긴다 — 사람이 엔터를 누르기를 기다리는 시간이 특히 그렇다.
    """
    packets = 0
    # 해제 → 정지 유지 → 구동. 세 구간 사이에 침묵이 없어야 한다.
    commander.once("RESET_SAFE")
    commander.halt()
    if settle_s > 0:
        _, sent = pump(sock, peer, commander, settle_s, Acks())
        packets += sent
    commander.drive(step_mm, angle_deg)
    # ⚠️ **측정 구간의 ACK 만 센다.** 정지 구간까지 세면 `applied` 가 섞여
    # "로봇이 걸었다" 를 정지 명령으로 채울 수 있다.
    acks = Acks()
    actual, sent = pump(sock, peer, commander, seconds, acks)
    packets += sent
    commander.halt()
    for line in commander.tick(system_clock_ms()):
        sock.sendto(line.encode("utf-8"), peer)
        packets += 1
    return actual, packets, acks


def _ask_number(prompt: str, *, allow_blank: bool, allow_zero: bool = False) -> float | None:
    while True:
        raw = input(prompt).strip()
        if not raw:
            if allow_blank:
                return None
            return None
        try:
            value = float(raw)
        except ValueError:
            print("    숫자를 입력한다")
            continue
        if value < 0 or (value == 0 and not allow_zero):
            print("    0 보다 커야 한다" if not allow_zero else "    음수는 안 된다")
            continue
        return value


#: 이 각도를 넘는 "방향 변화" 는 **거리와 순서를 바꿔 입력한 것**으로 본다.
#: 한 바퀴를 돌려면 실측 비율로 100초가 걸리므로 12초 창에서는 나올 수 없다.
DEGREE_TYPO_LIMIT = 360.0


def looks_swapped(mode: str, primary: float, secondary: float | None) -> bool:
    """주·부 측정이 **뒤바뀐 것처럼 보이는가.**

    실제로 겪었다 — 12초 우선회 3회 중 2회에서 각도 자리에 거리(1070mm)를,
    거리 자리에 각도(43도)를 넣었다. 도구는 그대로 받아 **평균 60.5 도/s ·
    퍼짐 82%** 를 냈고, `--write` 였다면 그 값이 프로파일에 적혔다.

    ⚠️ **퍼짐 경고만으로는 부족하다.** 세 시행을 다 같은 순서로 뒤바꿔 넣으면
    퍼짐이 작아서 경고가 뜨지 않는다 — 조용히 각도와 거리가 맞바뀐 값이 남는다.
    """
    _what, unit, _second_what, second_unit = MEASURES[mode]
    if unit == "도" and abs(primary) > DEGREE_TYPO_LIMIT:
        return True
    return second_unit == "도" and secondary is not None and abs(secondary) > DEGREE_TYPO_LIMIT


def discard_reason(acks: Acks, host: str) -> str | None:
    """이 시행을 버려야 하는 이유. 쓸 만하면 `None`.

    ⚠️ **응답이 하나도 없는 것을 통과시키고 있었다.** `응답 없음` 을 *"목업이거나
    ACK 미지원"* 으로 넘기고 측정값을 그대로 물었다 — 실기 주소가 비어 있으면
    패킷이 아무 데도 도착하지 않는데 **사람은 그것을 모르고 줄자 값을 넣는다.**
    2026-09-12 에 실제로 겪었다: 전원을 다시 켠 로봇이 DHCP 로 다른 주소를 받아
    갔고, 도구는 조용히 빈 주소로 10초를 쏜 뒤 거리를 물었다.

    목업(`127.*`)은 ACK 를 주지 않아도 정상이므로 그쪽만 통과시킨다.
    """
    if acks.total == 0:
        if host.startswith("127."):
            return None
        return (
            f"**{host} 가 한 번도 응답하지 않았다.** 주소가 바뀌었거나 꺼져 있다 — "
            "패킷이 아무 데도 도착하지 않았으므로 로봇은 한 발도 움직이지 않았다."
        )
    if acks.applied == 0:
        # 응답은 오는데 하나도 적용하지 않았다. 침묵 300ms 로 래치가 걸린 경우다.
        return (
            f"**로봇이 적용한 명령이 0개다.** 래치 {acks.latched}회 · "
            f"폐기 {acks.discarded}회 — 침묵이 300ms 를 넘었거나 원인이 남아 있다."
        )
    return None


def ask_measurement(mode: str, index: int, total: int) -> tuple[float, float | None] | None:
    """주 측정과 부 측정을 받는다. 주 측정이 비면 **그 시행을 버린다.**

    부 측정은 비워도 된다 — 재지 못한 것을 0 으로 채우면 *"쟀다"* 가 되므로
    `None` 으로 남긴다.

    뒤바뀐 것처럼 보이면 **한 번 되묻는다.** 같은 값을 두 번 넣으면 그대로 쓴다
    — 사람이 확인한 것이므로 도구가 더 고집할 일이 아니다.
    """
    what, unit, second_what, second_unit = MEASURES[mode]
    asked_again = False
    while True:
        primary = _ask_number(
            f"  [{index}/{total}] {what}({unit})? 엔터만 치면 이 시행을 버린다: ",
            allow_blank=True,
        )
        if primary is None:
            return None
        # ⚠️ 방향 변화는 **0 일 수 있다** (완벽히 직진). 거리는 0 이면 안 움직인 것이다.
        secondary = _ask_number(
            f"       {second_what}({second_unit})? 안 쟀으면 엔터: ",
            allow_blank=True,
            allow_zero=(second_unit == "도"),
        )
        if asked_again or not looks_swapped(mode, primary, secondary):
            return primary, secondary
        asked_again = True
        print(
            f"    ⚠️ {DEGREE_TYPO_LIMIT:.0f}도를 넘는 방향 변화다 — **{what}({unit})과 "
            f"{second_what}({second_unit})의 순서가 바뀐 것 아닌가?**\n"
            "       다시 입력한다. 같은 값을 넣으면 그대로 쓴다."
        )


def summarize(
    trials: list[Trial], mode: str, config: Mapping[str, Any] | None = None
) -> float | None:
    """평균 비율을 낸다. **퍼짐이 크면 경고하고, 부 측정도 함께 보고한다.**"""
    what, unit, second_what, second_unit = MEASURES[mode]
    rate_unit, second_rate_unit = f"{unit}/s", f"{second_unit}/s"
    if not trials:
        print("\n쓸 수 있는 시행이 없다.")
        return None
    print(
        f"\n{'시행':>4} {'요청':>7} {'실제창':>8} {'패킷':>5} "
        f"{what:>9} {'비율':>11} {second_what:>9} {'비율':>10}"
    )
    for t in trials:
        second = "—".rjust(9) if t.secondary is None else f"{t.secondary:>9.1f}"
        second_rate = (
            "—".rjust(10)
            if t.secondary_rate is None
            else f"{t.secondary_rate:>6.1f} {second_rate_unit}"
        )
        print(
            f"{t.index:>4} {t.requested_s:>6.2f}s {t.actual_s:>7.3f}s {t.packets:>5} "
            f"{t.measured:>9.1f} {t.rate:>7.1f} {rate_unit} {second} {second_rate}"
        )
    rates = [t.rate for t in trials]
    mean = statistics.fmean(rates)
    print(f"\n  평균 {mean:.1f} {rate_unit}  (n={len(rates)})")
    if len(rates) >= 2:
        spread = statistics.stdev(rates)
        print(f"  표준편차 {spread:.1f} {rate_unit} ({spread / mean * 100:.0f}%)")
        if spread > mean * SPREAD_WARN:
            print(
                f"  ⚠️ 퍼짐이 {SPREAD_WARN * 100:.0f}% 를 넘는다 — **바닥이 일정하지 않다.**\n"
                "     이 바닥에서는 회피 시퀀스의 후진·선회량을 신뢰할 수 없다.\n"
                "     시연할 바닥에서 다시 재거나 시행을 늘린다."
            )
    if len(rates) < 3:
        print("  ⚠️ WBS 2.2.3 은 **3회 이상 평균**을 요구한다.")
    second_mean = secondary_mean(trials)
    if second_mean is not None:
        counted = sum(1 for t in trials if t.secondary_rate is not None)
        print(f"  부 측정 평균 {second_mean:.1f} {second_rate_unit}  (n={counted})")
        if mode.startswith("turn") and config is not None:
            report_avoid_clearance(mean, second_mean, config)
    return mean


def secondary_mean(trials: Sequence[Trial]) -> float | None:
    """부 측정의 평균. **잰 시행만 센다** — 안 잰 것을 0 으로 채우면 *"쟀다"* 가 된다.

    후진 선회에서는 이 값이 **설정에 들어간다**(`reverse_turn_mm_per_sec`). 회피가
    각도와 여유 두 목표를 비교하려면 후퇴 속도가 있어야 하기 때문이다.
    """
    rates = [t.secondary_rate for t in trials if t.secondary_rate is not None]
    return statistics.fmean(rates) if rates else None


def report_avoid_clearance(
    deg_per_sec: float, mm_per_sec: float, config: Mapping[str, Any]
) -> None:
    """회피가 **실제로 장애물을 벗어나는지** 산수로 보여 준다.

    ⚠️ **제자리 회전이 불가하므로**(ADR-11) 선회 구간이 앞으로 나아간다. 후진으로
    확보한 여유를 그만큼 되돌려 주며, 되돌려 주는 양이 더 크면 **같은 장애물에 다시
    붙는다** — `avoid_phases` 가 계산하는 것은 시간뿐이라 이 산수를 하지 않는다.
    """
    increment = float(config["localization"]["turn_increment_deg"])
    reverse_mm = float(config["gait"]["reverse_distance_mm"])
    if deg_per_sec <= 0:
        return
    turn_s = increment / deg_per_sec
    advance = mm_per_sec * turn_s
    net = reverse_mm - advance
    print(
        f"\n  회피 산수 — 후진 {reverse_mm:.0f}mm 확보 후 {increment:.0f}도 선회에 "
        f"{turn_s:.2f}초, 그 동안 전진 {advance:.0f}mm"
    )
    print(f"  → **순 여유 {net:.0f}mm**")
    if net <= 0:
        print("  ⚠️ **여유가 음수다 — 회피가 장애물에 더 붙는다.** 설정을 고쳐야 한다:")
        print(
            f"     `gait.reverse_distance_mm` 를 {advance * 1.5:.0f}mm 이상으로 올리거나 "
            "`localization.turn_increment_deg` 를 줄인다"
        )
    elif net < reverse_mm * 0.3:
        print(f"  ⚠️ 확보량의 {net / reverse_mm * 100:.0f}% 만 남는다 — 여유가 얇다")


def write_profile(
    device: str,
    mode: str,
    value: float,
    *,
    secondary: float | None = None,
    root: Path = Path("config/devices"),
) -> Path | None:
    """개체 프로파일에 적는다. **`measured_on` 을 함께 적는다.**

    `root` 를 인자로 받는 것은 **시험 때문이다** — 실제 프로파일을 고치지 않고
    임시 사본으로 검증한다.

    ⚠️ `prod` 프로파일은 두 값과 `measured_on` 이 모두 있어야 기동한다
    (`config.py` 의 `validate_device_config`). 날짜 없이 숫자만 넣으면 *"언제 어느
    바닥에서 잰 값인가"* 를 잃고, 그러면 다시 재야 하는지 알 수 없다.
    """
    path = root / f"{device}.yaml"
    key = PROFILE_KEYS.get(mode)
    if key is None:
        # ⚠️ **우선회는 적을 곳이 없다.** 회피는 `+turn_angle_deg`(좌선회)만 쓰고
        # 설정에도 방향별 선회 키가 없다. 값을 임의로 적으면 *"쟀다"* 로 보이지만
        # **어느 방향의 값인지 알 수 없게 된다.** `3.5.4` TRACK 락온이 양쪽을 쓰게
        # 되면 그때 키를 만든다.
        print(
            f"⚠️ `{mode}` 는 설정에 적을 키가 없다 — 좌선회 값과 비교해 차이가 크면 "
            "방향별 선회 키를 새로 만들어야 한다",
            file=sys.stderr,
        )
        return None
    second_key = SECONDARY_KEYS.get(mode)
    if second_key is not None and secondary is None:
        # ⚠️ **반만 적으면 개체가 조용히 옛 구간표로 돌아간다.** 두 목표를 비교할
        # 수 없으므로 `avoid_phases` 가 새 구간을 만들지 않는다 — 적힌 값은 있는데
        # 쓰이지 않는, 가장 알아채기 어려운 상태가 된다.
        print(
            f"⚠️ `{mode}` 는 `{key}` 와 `{second_key}` 가 **둘 다** 있어야 회피에 쓰인다 — "
            "부 측정이 없어 적지 않는다",
            file=sys.stderr,
        )
        return None
    writes = {key: value}
    if second_key is not None and secondary is not None:
        writes[second_key] = secondary
    text = path.read_text(encoding="utf-8")
    today = time.strftime("%Y-%m-%d")
    lines = text.splitlines(keepends=True)
    touched: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        for name, number in writes.items():
            if stripped.startswith(f"{name}:"):
                lines[i] = f"{indent}{name}: {number:.1f}\n"
                touched.add(name)
        if stripped.startswith("measured_on:"):
            lines[i] = f'{indent}measured_on: "{today}"\n'
    missing = [name for name in writes if name not in touched]
    if missing:
        print(f"⚠️ {path} 에서 `{missing}` 줄을 찾지 못했다 — 손으로 적는다", file=sys.stderr)
        return None
    path.write_text("".join(lines), encoding="utf-8")
    written = " · ".join(f"{name}: {number:.1f}" for name, number in writes.items())
    print(f"\n{path} 에 {written} · measured_on: {today} 를 적었다")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gait_calibrate", description="보행 이동량 실측")
    parser.add_argument("--device", default="mechdog-01", help="개체 id")
    parser.add_argument("--host", required=True, help="로봇 IP (목업이면 127.0.0.1)")
    parser.add_argument(
        "--mode",
        choices=("forward", "reverse", "turn_right", "turn_left", "reverse_turn"),
        required=True,
    )
    parser.add_argument("--seconds", type=float, default=3.0, help="한 시행의 구동 시간")
    parser.add_argument("--trials", type=int, default=3, help="시행 횟수 (2.2.3 은 3회 이상)")
    parser.add_argument(
        "--bias-deg",
        type=float,
        default=0.0,
        help="모든 모드의 angle 에 더한다 — 직진 요 편향 보정값을 찾을 때 쓴다",
    )
    parser.add_argument("--write", action="store_true", help="개체 프로파일에 결과를 적는다")
    parser.add_argument(
        "--dry-run", action="store_true", help="측정 입력 없이 송신 창만 확인한다 (목업 연습)"
    )
    return parser


def command_for(
    mode: str, gait: Mapping[str, Any], *, bias_deg: float = 0.0
) -> tuple[float, float]:
    """모드 → 보낼 `move(step, angle)`. **부호가 이 표에 다 들어 있다.**

    ⚠️ **양수 = 반시계 = 좌회전** (PROTOCOL 부호 규약, 2026-09-11 실물 확인).
    처음에는 *"양수가 우선회"* 라고 적었고 근거는 `teleop` 의 `right: +1.0` 이었다
    — **그 teleop 이 좌우가 뒤바뀐 상태였다.** 회피는 `+turn_angle_deg` 를 그대로
    쓰므로 **좌선회 한 방향만** 쓰고, 양쪽을 재는 이유는 `3.5.4` TRACK 락온이
    x편차 비례로 둘 다 쓰며 서보 오프셋이 비대칭이기 때문이다.

    ⚠️ **요는 `angle` 단독으로 결정된다 — `step × angle` 이 아니다.**
    2026-09-11 실물: `move(-60,+20)` 은 엉덩이가 오른쪽으로 가며 후진했고(코는
    왼쪽 = 반시계), `move(-60,-20)` 은 그 반대였다. `patrol.steering_for` 주석과
    `slam.simulation.apply_move` 는 `sign(step)` 을 곱하고 있었는데 **그 가정이
    반증됐다** — 처음에는 그 주석을 근거로 여기서도 음수를 보냈다.

    `bias_deg` 는 **직진 요 편향 보정값을 찾기 위한** 것이다. 찾는 것은 *"방향
    변화가 0 이 되는 각도"* 이고, 그 값이 나오면 순찰의 직진 명령이 그 각도를
    실어 보내면 된다. ⚠️ 작은 각도의 응답이 선형인지는 모른다 — `angle=20` 이
    6.8 도/s 니 1 도/s 는 약 -3 이겠지만 **데드밴드가 있으면 아무 변화도 없다.**
    그것을 확인하는 것도 이 측정의 목적이다.
    """
    step = float(gait["step_length_mm"])
    if mode in ("reverse", "reverse_turn"):
        step = -step
    turn = float(gait["turn_angle_deg"])
    angle = {"turn_left": turn, "turn_right": -turn, "reverse_turn": turn}.get(mode, 0.0)
    return step, angle + float(bias_deg)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 실기 측정용
    # ⚠️ **가장 먼저 부른다.** cp949 콘솔에서 `⚠️` 가 있는 첫 `print` 가 죽는다.
    survive_encoding_errors()
    args = build_parser().parse_args(argv)
    config = load_config(args.device)
    network = config["network"]
    step, angle = command_for(args.mode, config["gait"], bias_deg=args.bias_deg)
    peer = (args.host, int(network["cmd_port"]))
    period_ms = 1000 // int(network["cmd_rate_hz"])

    print(f"개체 {args.device} · {peer[0]}:{peer[1]} · {args.mode}")
    print(f"명령 move({step:g}, {angle:g}) · 한 시행 {args.seconds:g}초 · {args.trials}회")
    if args.mode.startswith("turn"):
        print(
            "⚠️ 제자리 회전은 불가하다 (ADR-11) — **원호로 돌면서 앞으로도 간다.**\n"
            "   각도를 입력하되 **시작·끝 위치도 함께 표시**해 두면 회피 선회 구간의\n"
            '   실제 이동을 나중에 알 수 있다 (`Phase("turn", +60mm, ±20°)`).'
        )
    if args.mode not in PROFILE_KEYS:
        print(f"⚠️ `{args.mode}` 는 설정에 적을 키가 없다 — 가정 확인용 측정이다")
    if args.dry_run and not args.host.startswith("127."):
        # ⚠️ **연습 실행도 실제로 걷는다.** `--dry-run` 이 건너뛰는 것은 *사람의
        # 측정 입력*뿐이고 `MOVE` 는 그대로 나간다 — 목업으로 명령 부호를 확인한
        # 것이 이 경로였다. 그래서 **엔터 대기도 없다**: 실기에 걸면 아무도 붙잡고
        # 있지 않은 상태로 명령한 시간만큼 걷는다.
        print(
            f"⚠️ **연습 실행이지만 {args.host} 로 `MOVE` 를 그대로 보낸다** — 로봇이\n"
            "   켜져 있으면 붙잡는 사람 없이 걷는다. 목업(127.0.0.1)에서만 쓴다."
        )
    print("⚠️ 시연할 바닥에서 잰다. 카펫과 장판에서 값이 다르다.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # ACK 를 막지 않고 읽는다 — 측정 주기를 ACK 대기로 흔들면 안 된다.
    sock.setblocking(False)
    commander = Commander(CommandEncoder(clock=system_clock_ms), period_ms=period_ms)
    sock.sendto(commander.open_session().encode("utf-8"), peer)
    # ⚠️ **래치 해제를 여기서 한 번 보내면 안 된다.** 로봇은 안전 상태로 깨어나고
    # (`g_safe_latched = true`), 300ms 침묵마다 다시 잠긴다. 사람이 엔터를 누르기를
    # 기다리는 시간이 그 침묵이므로, 해제는 **각 시행의 구동 직전**에 보낸다
    # (`drive_window`). 실제로 기동 때 한 번만 보내도록 만들었고, 로봇은 ACK 를
    # `ACCEPT` 로 주면서 **한 발도 움직이지 않았다** — `applied` 가 `false` 였다.

    trials: list[Trial] = []
    try:
        for index in range(1, args.trials + 1):
            if not args.dry_run:
                input(f"  [{index}/{args.trials}] 시작 위치를 표시하고 엔터")
            print(f"    정지 {SETTLE_S:g}초 유지 후 {args.seconds:g}초 구동")
            # ⚠️ **`time.sleep` 으로 기다리지 않는다.** 그러면 300ms 워치독이 걸려
            # 로봇이 다시 잠기고 뒤따르는 `MOVE` 가 전부 무시된다 — 실제로 그렇게
            # 만들어서 **한 발도 움직이지 않았다.** 정지를 10Hz 로 보내며 기다린다.
            actual, packets, acks = drive_window(
                sock,
                peer,
                commander,
                step_mm=step,
                angle_deg=angle,
                seconds=args.seconds,
                settle_s=SETTLE_S,
            )
            print(f"    실제 송신 창 {actual:.3f}초 · 패킷 {packets}개 · {acks.describe()}")
            if args.dry_run:
                continue
            reason = discard_reason(acks, args.host)
            if reason is not None:
                print(f"    ⚠️ {reason}\n       이 시행을 버린다.")
                continue
            answer = ask_measurement(args.mode, index, args.trials)
            if answer is None:
                print("    버렸다")
                continue
            measured, secondary = answer
            trials.append(Trial(index, args.seconds, actual, packets, measured, secondary))
    except KeyboardInterrupt:
        print("\n중단됐다")
    finally:
        sock.sendto(commander.emergency_stop().encode("utf-8"), peer)
        sock.close()

    if args.dry_run:
        print("\n연습 실행이었다 — 값을 내지 않는다")
        return 0
    mean = summarize(trials, args.mode, config)
    if mean is None:
        return 1
    if args.write:
        if args.bias_deg:
            # ⚠️ **보정 각도를 준 측정은 프로파일 값이 아니다.** `forward_mm_per_sec`
            # 은 *보정 없는* 직진 속도를 뜻하며, 여기서 나온 값을 적으면 이름과
            # 내용이 어긋난 채로 회피 시간 계산에 들어간다.
            print(
                f"\n⚠️ `--bias-deg {args.bias_deg:g}` 을 준 측정은 적지 않는다 — "
                "프로파일 값은 보정 없는 직진 기준이다",
                file=sys.stderr,
            )
            return 1
        if len(trials) < 3:
            print("\n⚠️ 3회 미만이므로 적지 않는다 (WBS 2.2.3)", file=sys.stderr)
            return 1
        write_profile(args.device, args.mode, mean, secondary=secondary_mean(trials))
    else:
        key = PROFILE_KEYS.get(args.mode)
        if key is None:
            print("\n적을 키가 없는 모드다 — 전진·우선회 값과 비교한다")
            return 0
        print(f"\n적으려면 --write. 손으로 적으려면 `{key}: {mean:.1f}`")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
