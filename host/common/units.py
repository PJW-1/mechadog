"""전선 단위와 내부 단위의 경계 (PROTOCOL.md 2절 · 5절).

**규약은 mm·deg·cm·ms 를 쓰고 이 패키지 안은 m·rad·s 를 쓴다.** 어느 한쪽으로
통일하지 않는 이유가 양쪽 모두에 있다.

* 전선을 바꿀 수 없다 — `step` 은 mm, `imu` 는 deg, `dist_cm` 은 cm 로 이미
  못박혀 있고 C++ 파서가 그 단위로 구현되어 있다 (파괴적 변경 · PROTOCOL 4절).
* 내부를 mm·deg 로 두면 삼각함수를 부를 때마다 변환이 흩어진다. 점유격자의
  해상도(`0.05 m`)와 A* 비용도 m 로 계산해야 자연스럽다.

그래서 **변환은 이 파일에만 있다.** 곱셈 하나짜리 함수를 굳이 이름 붙여 두는
이유는, 변환이 코드 곳곳에 흩어지는 순간 어느 값이 어느 단위인지 아무도 모르게
되기 때문이다 — 그때 생기는 버그는 1000배나 57배로 틀리므로 조용하지 않고,
대신 **왜 틀렸는지 찾기가 어렵다.**
"""

from __future__ import annotations

import math

MM_PER_M: float = 1000.0
CM_PER_M: float = 100.0
MS_PER_S: float = 1000.0


# ── 길이 ──────────────────────────────────────────────────────
def mm_to_m(mm: float) -> float:
    return mm / MM_PER_M


def m_to_mm(m: float) -> float:
    return m * MM_PER_M


def cm_to_m(cm: float) -> float:
    """`dist_cm`(초음파) 전용. 텔레메트리는 cm 로 온다."""
    return cm / CM_PER_M


def m_to_cm(m: float) -> float:
    return m * CM_PER_M


# ── 각도 ──────────────────────────────────────────────────────
def deg_to_rad(deg: float) -> float:
    return math.radians(deg)


def rad_to_deg(rad: float) -> float:
    return math.degrees(rad)


# ── 시각 ──────────────────────────────────────────────────────
def ms_to_s(ms: float) -> float:
    return ms / MS_PER_S


def s_to_ms(s: float) -> int:
    """규약의 `ts` 는 **정수 밀리초**다 (초도 실수도 아니다).

    합친 코드에서 실제로 틀렸던 지점이다 — `time.time()` 을 그대로 실어 보내면
    `ts` 가 초 단위 실수가 되어 규칙 ⑤(정수 아님)로 폐기된다.
    """
    return int(s * MS_PER_S)


# ── 각도 정규화 ────────────────────────────────────────────────
def wrap_pi(rad: float) -> float:
    """`[-pi, pi)` 로 접는다. 방위 오차를 계산할 때마다 필요하다.

    경계가 `-pi` 쪽에 닫혀 있다 — 정확히 반대 방향은 `-pi` 로 나온다. 부호가
    어느 쪽이든 크기는 같으므로 조향 판단에는 영향이 없지만, 시험에서
    `+pi` 를 기대하면 틀린다.
    """
    return (rad + math.pi) % (2 * math.pi) - math.pi
