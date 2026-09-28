"""TRACK 락온 제어기 — bbox 중심 x편차를 선회 보행으로 바꾼다 (WBS 3.5.4 · FR-3.5).

`ALERT` 에서 타겟이 화면 중앙을 벗어나면 `TRACK` 으로 가고, 이 파일이 **얼마나
돌지**를 정한다. FSM 은 *언제* 도는지만 안다 — `TARGET_OFF_CENTER` 와
`TARGET_CENTERED` 는 전이표에 있지만 그것을 **내는 쪽이 없었다.** 여기가 그 자리다.

**제자리 회전을 전제하지 않는다** (DR-11). 그래서 조향은 곧 걷기다 — `MOVE` 의 `angle`
만 주고 `step` 을 0 으로 두면 로봇은 아무 데도 향하지 못한다. 편차가 남아 있는
동안은 **걸으면서** 방향을 맞춘다.

⚠️ **2026-09-23 부터 예외가 있다** (ADR-40). 정지선에 닿았거나 편차가 크면 `runtime._track` 이
이 지시를 **제자리 회전(20°/30°)** 으로 덮어쓴다. 이 클래스는 호 추종만 계산한다.

    화면                     보내는 것
    ├─────┼──╳──┤            타겟이 오른쪽  → angle < 0 (우선회) · step 전진
    ├──╳──┼─────┤            타겟이 왼쪽    → angle > 0 (좌선회) · step 전진
    ├───╳═╪═╳───┤            데드존 안       → 정지, TARGET_CENTERED
         데드존

(이 부호는 **보정 전** 이야기다. 아래의 직진 편향을 더하면 보내는 값이 통째로
밀리므로, 왼쪽 타겟에도 음수 `angle` 이 나갈 수 있다 — 그래도 **실제로 도는 방향은
왼쪽**이다. 드리프트를 갚고 남는 것이 조향이기 때문이다.)

⚠️ **부호를 뒤집으면 타겟에서 멀어진다.** `PROTOCOL` 부호 규약은 *양수 = 반시계 =
로봇의 좌회전* 이다. 화면 좌표 x 는 오른쪽으로 커지므로, 타겟이 오른쪽에 있으면
편차가 **양수**이고 로봇은 **우회전**해야 한다 — 즉 `angle` 은 **음수**다. 편차
부호와 명령 부호가 반대라는 것이 이 파일에서 가장 틀리기 쉬운 곳이다.

⚠️ **데드존 경계에서 명령이 튀지 않게 한다.** 편차를 그대로 비례시키면 데드존을
벗어나는 순간 조향각이 0 에서 갑자기 솟는다. 그래서 **데드존을 뺀 나머지**를
비례 구간으로 쓴다 — 경계에서 0 으로 시작해 화면 끝에서 최대가 된다. 데드존은
*"흔들지 않는 구간"* 이지 *"넘으면 확 꺾는 문턱"* 이 아니다.

⚠️ **경계 히스테리시스는 넣지 않았다.** bbox 중심이 데드존 경계에 딱 걸치면
`TRACK` 과 `ALERT` 사이를 오갈 수 있다. 막으려면 진입·이탈 임계를 따로 둬야
하는데, **그 간격을 정하려면 실제 bbox 중심이 얼마나 떨리는지를 재야 한다.**
재지 않은 값을 넣으면 좁아서 무용하거나 넓어서 추종이 굼떠진다. 실측 후 `config`
에 임계를 추가하는 것이 순서다 (`gait_calibration` 이 없으면 회피 구간을 만들지
않는 `actions.avoid_phases` 와 같은 원칙).

⚠️ **직진 드리프트를 조향에 함께 싣는다 (2026-09-18).**

**아래 숫자는 전부 `mechdog-01` 한 대의 실측이다 — 다른 기체에 쓰면 안 된다.**
비대칭의 출처가 개체별 서보 오프셋이라 **기체마다 방향도 크기도 다르게 나온다**
(HARDWARE 3절 · 복사 금지). 코드는 숫자를 박지 않고 그 기체의
`gait_calibration.straight_bias_deg` 를 읽으며, 값이 없는 기체는 **보정하지 않는다.**

`mechdog-01` 은 걷기만 해도 좌로 **1.0 °/s** 휘고, 우선회에는 약 **3.3°** 의
데드밴드가 있다 (`config/devices/mechdog-01.yaml` · 2026-09-11~12 실측). 둘이
겹치면 좌우가 이렇게 갈린다 —

    명령 +0.77°(좌)  데드밴드에 먹혀 조향 0   + 드리프트 좌 1.0 =  좌 1.00 °/s
    명령 -8.40°(우)  0.228 x (8.40-3.3)=1.16 - 드리프트 좌 1.0 =  우 0.16 °/s

**왼쪽은 명령이 0 이 돼도 드리프트가 대신 돌려주고, 오른쪽은 드리프트를 먼저
갚느라 남는 것이 없다.** 2026-09-18 실기에서 오른쪽 대상을 49초 쫓고도 편차가
158 -> 233px 로 벌어졌고, 같은 날 왼쪽은 183 -> 11px 로 12초 만에 들어왔다.

`gait_calibration.straight_bias_deg` 가 그 드리프트를 0 으로 만드는 값이며 **이미
실측돼 있었다 — 쓰는 곳이 없었을 뿐이다.** 조향에 더하면 드리프트가 상쇄되고,
총 명령이 데드밴드 밖으로 밀려나 **비례 제어의 죽은 저역까지 함께 살아난다.**

⚠️ **중앙에 들어오면 더하지 않는다** — 그때는 `step=0` 이라 걷지 않고, 걷지 않으면
드리프트도 없다. 서 있는 로봇에 편향만 주면 제자리에서 돌라는 말이 된다.

⚠️ **거스르는 쪽에만 더한다 — 정정 (2026-09-18 재검증).** 처음에는 좌우 구분 없이
더했고 오른쪽은 실제로 살아났다(212 -> 35px · 34초). 그런데 **왼쪽이 죽었다.** 좌선회
명령은 작을 때 보정에 부호가 뒤집혀 **우 데드밴드 안**으로 들어가 버린다 —

    dev -112.6px  raw +5.19°(좌) + (-8.0) = -2.81°  ->  |2.81| < 3.3  ->  조향 0

왼쪽이 실제로 꺾이려면 `raw - 8 > 3.3` 이라 **편차가 198px 을 넘어야 했다.** 데드존을
40px 로 정해 놓고 한쪽만 조용히 198px 로 키운 셈이다. 드리프트가 좌라면 그것을 갚아야
하는 것은 **우선회뿐**이고, 좌선회는 드리프트가 같은 방향이라 이미 덤을 받고 있다.

⚠️ **위 «데드밴드 3.3°» 는 틀렸다 — 정정 (2026-09-18 곡선 실측 · mechdog-01).**
조향 응답이 **0 을 연속으로 통과한다.** 평평한 구간이 없고, 회전이 0 이 되는 각도는
**-5.4°** 다(`gait_calibration.yaw_zero_angle_deg`). 그래서 —

- *"3도 이하 명령은 아무 일도 하지 않는다"* → **한다.** 다만 드리프트(좌 **1.87 °/s**,
  기록된 1.0 의 1.9배)가 더 커서 **순 회전이 여전히 좌**다. 2026-09-18 오전에 왼쪽이
  죽은 것은 조향이 0 이 돼서가 아니라 **그때 보정값 `-8.0` 이 영점(-5.0)을 3도 넘겨**
  좌선회 명령을 우쪽으로 밀었기 때문이다. 결과는 같고 기전이 달랐다.
- **보정값을 실측 영점 `-5.0` 으로 내렸다** (2026-09-18 직진 3점: `0 → +1.87` ·
  `-5 → -0.19` · `-8 → -1.40`). `-8.0` 은 두 점 눈대중 보간이었고, 그 값으로 걸으면
  순찰 직진이 **우 1.40 °/s** 로 5m 에 67도 휜다.

⚠️ **«좌측에도 보정» 은 실기로 기각됐다 (2026-09-19 · `mechdog-01`).** 곡선 대입으로는
양방향이 편차 110px 에서 1.02배로 더 대칭이라고 나왔는데, 실기에서는 **5.0배**로 벌어졌다
(배터리 7.59 / 7.56V 로 좌우 조건을 맞춘 한 런) —

    우  편차 109px  명령 -4.96 → -9.96 (2.01배)  회전 -4.02 °/s  이득 0.811
    좌  편차 168px  명령 +9.16 → +4.16 (0.45배)  회전 +1.49 °/s  이득 0.163

좌측은 **편차가 더 큰데 회전이 더 느렸고**, 7초 동안 163~175px 에 붙박여 수렴하지 않았다.

⚠️ **계산이 틀린 이유를 남긴다 — 곡선이 아니라 곡선에 넣은 입력이 틀렸다.** 대입은
*"명령 → 회전율"* 만 봤는데 양방향은 그 **앞단에서 명령 자체를 가른다**(위 2.01배 대 0.45배).
명령 단계에서 이미 2.4:1 로 벌어진 것을 곡선이 다시 벌린다. **모델을 바꿀 때는 «명령
단계» 와 «회전율 단계» 를 나눠 봐야 한다.**

⚠️ **그리고 우선회는 두 방식이 «완전히 같은 명령» 을 낸다** — 부호가 같아 어차피 더해지기
때문이다. 즉 양방향은 **우측을 전혀 개선하지 못하면서 좌측만 깎는다.** 여기 있는 방식이
이긴 것이 아니라 상대가 잃을 것밖에 없었다.

⚠️ **아직 모르는 것 — 이 방식의 «좌측» 이득.** 오늘 좌측은 양방향으로만 쟀다. 그리고
같은 런에서 **곡선과 추종이 어긋났다**(곡선은 영점 기준 좌우 0.27~0.34 로 대칭인데 추종은
0.811 대 0.163). 배터리 바닥(7.56V)인지 과도응답인지 미확인이다. 그것이 갈리기 전에는
좌/우 이득 분리 같은 노브를 늘리지 않는다.
상세와 절차는 `docs/measurements/2026-09-18-turn-rate-curve.md` 와
`TEST_MECHDOG/results/20260919_bias-symmetry/summary.md`.

## 거리 유지 (FR-3.5.2 · `3.5.8`)

bbox **높이**로 한다 — 미터로 바꾸지 않는다 (DR-15). 목표 높이에 가까워질수록
보폭을 줄이고, 정지 배율을 넘으면 0 으로 세운다.

⚠️ **최소 보폭을 0 으로 두면 안 된다 — 전진이 없으면 조향도 죽는다.** 제자리 선회가
불가하므로(DR-11) 호로만 돌고 호는 전진이 있어야 생긴다. 즉 **거리 유지와 정렬이 같은
노브를 쓴다.** 목표 거리에 닿았는데 아직 정렬이 안 됐으면 최소 보폭으로 계속 돈다
(2026-09-19 결정). 후진은 선택지가 아니다 — 25cm 를 막은 것이 **대상 본인일 수 있고**
그때 물러나는 것은 경비 로봇으로서 틀린 행동이다 (ARCHITECTURE 설계 규칙 ④).

⚠️ **목표 높이가 없으면 거리 제어를 하지 않는다** — `straight_bias_deg` 와 같은 규칙이고
**추정값을 넣지 않는다.** 화각·장착 높이·사람 키가 섞인 값이라 계산으로 세우면 틀린다.
1초 요약의 `track_box_h_px` 를 목표 거리에서 읽어 채운다.

⚠️ **이것이 없으면 접근을 막는 것이 온보드 초음파(25cm)뿐이다** — 2026-09-19 실기에서
로봇이 사람 코앞까지 왔다. **Tier 1 안전 반사가 거리 제어를 대신하는 상태**이며 반사는
마지막 방어선이지 제어 루프가 아니다.

⚠️ **가까워질수록 정렬이 어려워진다.** 같은 횡방향 거리가 3m 에서 9.5°, 0.5m 에서 45°
이므로 **접근할수록 편차가 커진다.** 거리 유지가 없으면 로봇은 영영 정렬되지 않은 채
계속 전진한다 — 실기에서 본 것이 이 기하다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from host.common.protocol import CLAMP_RANGES

__all__ = ["TrackCommand", "LockOnTracker"]


@dataclass(frozen=True, slots=True)
class TrackCommand:
    """한 프레임의 추종 지시.

    `centered` 는 조향 데드존 판정이다. FSM 정지는 거리(`step=0`)가 가른다.
    """

    step: float
    """전진 보폭 (mm). 정지선에 닿으면 0 이다."""

    angle: float
    """arc 조향각 (deg). **양수가 좌회전** (PROTOCOL 부호 규약)."""

    centered: bool
    """데드존 안인가."""

    deviation_px: float
    """화면 중앙 대비 x 편차 (px). 오른쪽이 양수. 로그·대시보드 표시용."""


class LockOnTracker:
    """bbox 중심 x편차를 `MOVE` 의 `step`·`angle` 로 바꾼다.

    상태를 들고 있지 않다 — 같은 입력이면 항상 같은 출력이다. 추종이 끊긴 판단은
    `FR-3.7` 의 5초 타이머 소관이라 여기서 시간을 재지 않는다.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        fsm = config["fsm"]
        gait = config["gait"]
        deadzone = float(fsm["track_deadzone_px"])
        step = abs(float(gait["step_length_mm"]))
        turn = abs(float(gait["turn_angle_deg"]))
        if deadzone < 0:
            raise ValueError("track_deadzone_px 는 0 이상이어야 함")
        if step <= 0:
            raise ValueError("step_length_mm 이 0 이면 선회할 수 없다 (제자리 회전 미전제)")
        if turn <= 0:
            raise ValueError("turn_angle_deg 는 0 보다 커야 함")
        self._deadzone_px = deadzone
        self._step_mm = step
        self._max_turn_deg = turn
        # 보정값이 없는 기체는 **보정하지 않는다.** 다른 기체의 값을 빌려오면 방향도
        # 크기도 달라 오히려 더 비뚤어진다 (HARDWARE 3절 — 복사 금지).
        bias = (config.get("gait_calibration") or {}).get("straight_bias_deg")
        self._bias_deg = 0.0 if bias is None else float(bias)
        target_h = fsm.get("track_target_height_px")
        self._target_h_px = None if target_h is None else float(target_h)
        if self._target_h_px is not None and self._target_h_px <= 0:
            raise ValueError("track_target_height_px 는 0 보다 커야 함")
        self._stop_ratio = float(fsm["track_stop_ratio"])
        if self._stop_ratio <= 1.0:
            raise ValueError("track_stop_ratio 는 1.0 보다 커야 함")
        self._min_step_mm = float(fsm["track_min_step_mm"])
        if not 0.0 < self._min_step_mm <= self._step_mm:
            raise ValueError("track_min_step_mm 은 0 초과 step_length_mm 이하여야 함")

    @property
    def deadzone_px(self) -> float:
        return self._deadzone_px

    def _step_for(self, box_height: float | None) -> float:
        """목표 bbox 높이에 가까울수록 보폭을 줄인다 (FR-3.5.2 · `3.5.8`).

        ⚠️ **최소에서 멎지 0 으로 가지 않는다** — 전진이 없으면 조향도 죽는다(DR-11).
        완전히 세우는 것은 `track_stop_ratio` 를 넘었을 때뿐이다.
        """
        if self._target_h_px is None or box_height is None or box_height <= 0:
            return self._step_mm  # 목표 미측정 — 거리 제어를 하지 않는다
        ratio = float(box_height) / self._target_h_px
        if ratio >= self._stop_ratio:
            return 0.0
        if ratio <= 1.0:
            return self._step_mm  # 아직 멀다 — 최대로 간다
        # 목표(1.0)에서 정지선 사이를 최대 → 최소로 편다.
        t = (ratio - 1.0) / (self._stop_ratio - 1.0)
        return self._step_mm + t * (self._min_step_mm - self._step_mm)

    def update(
        self, center_x: float, frame_width: int, box_height: float | None = None
    ) -> TrackCommand:
        """한 프레임의 검출로 지시를 만든다.

        `center_x` 는 bbox 중심의 화면 x 좌표, `frame_width` 는 프레임 폭,
        `box_height` 는 bbox 높이다. 전부 픽셀이며 **미터로 바꾸지 않는다**
        (DR-15 · FR-3.5.2 와 같은 이유).

        ⚠️ **`box_height` 가 없으면 거리 제어만 빠지고 조향은 그대로다** — 높이를
        모른다고 추종을 멈추면 검출기가 박스를 못 주는 프레임마다 로봇이 선다.
        """
        if frame_width <= 0:
            raise ValueError("frame_width 는 1 이상이어야 함")

        midpoint = frame_width / 2.0
        deviation = float(center_x) - midpoint

        # 데드존 밖으로 나갈 수 없는 화면이면(데드존이 반폭 이상) 추종이 성립하지
        # 않는다. 조용히 정지시키지 않고 설정 오류로 드러낸다.
        if self._deadzone_px >= midpoint:
            raise ValueError("track_deadzone_px 가 화면 반폭 이상이라 추종할 수 없다")

        if abs(deviation) <= self._deadzone_px:
            return TrackCommand(
                step=(
                    self._step_for(box_height)
                    if self._target_h_px is not None and box_height is not None
                    else 0.0
                ),
                angle=0.0,
                centered=True,
                deviation_px=deviation,
            )

        # 데드존을 뺀 나머지를 0~1 로 편다. 경계에서 0 이므로 각이 튀지 않는다.
        span = midpoint - self._deadzone_px
        magnitude = min((abs(deviation) - self._deadzone_px) / span, 1.0)

        # 편차 부호와 조향 부호는 **반대다** — 위 주석 참조.
        angle = -magnitude * self._max_turn_deg if deviation > 0 else magnitude * self._max_turn_deg
        # 걷는 동안에만 드리프트가 생기므로 여기서만 더한다. 더한 뒤에는 규약 범위를
        # 넘을 수 있어 클램프한다 — 로봇은 범위 밖 `angle` 을 잘라서 받는다.
        #
        # ⚠️ **드리프트를 거스르는 쪽에만 더한다.** 드리프트는 한 방향으로만 휘므로
        # 같은 방향으로 도는 명령에는 도움이 이미 와 있다. 양쪽에 똑같이 더하면 그쪽
        # 명령의 부호가 뒤집혀 **반대쪽 데드밴드 안으로 밀려 들어가고, 기체가 그쪽으로
        # 아예 못 돈다.** 보정값의 부호가 곧 *"거스르는 방향"* 이라 부호가 같을 때만 더한다.
        if angle * self._bias_deg > 0:
            angle += self._bias_deg
        low, high = CLAMP_RANGES["angle"]
        angle = min(max(angle, low), high)
        return TrackCommand(
            step=self._step_for(box_height),
            angle=angle,
            centered=False,
            deviation_px=deviation,
        )
