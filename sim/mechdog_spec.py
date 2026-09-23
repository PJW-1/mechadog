"""MechDog 실측 스펙 — 시뮬 프록시의 유일한 정본.

수치 출처:
  - 치수·무게: Hiwonder 공식 사양 (214×126×138mm, 560g, 8DOF, 알루미늄)
  - 보행 속도: `docs/HARDWARE.md` 실측 (전진 104mm/s·후진 78mm/s @ step 60)
  - 카메라: `docs/measurements/2026-09-13-camera-fov.md` 실측
  - 명령 의미: `docs/PROTOCOL.md` — MOVE 는 호(arc) 조향, angle>0 = CCW(좌회전)

⚠️ 보행 물리를 흉내내지 않는다. 시뮬에서 기체는 키네마틱(kinematic)으로 미끄러지듯
움직인다 — 데이터·동선 목적에는 카메라 포즈와 궤적이 맞으면 충분하다.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MechDogSpec:
    """미터·라디안 단위. 소스는 위 주석."""

    # ── 형상 (Hiwonder 공식, powered-on 자세) ──────────────
    body_length_m: float = 0.214
    body_width_m: float = 0.126
    body_height_m: float = 0.138
    mass_kg: float = 0.560
    leg_count: int = 4

    # ── 카메라 (XIAO 가 아니라 본체에 달린 FPV 시야 — 09-13 실측) ──
    cam_height_m: float = 0.15  # 바닥에서 렌즈까지
    # 2026-09-22 상향 재장착 목표값 — PPE(헬멧·조끼)+person_down 탐지 최적화.
    # +15° 이면 1.6m 이상에서 1.8m 키 작업자 전신이 들어오고 바닥은 0.58m
    # 밖부터 보인다(쓰러진 사람 커버). 구 −7° 는 2m 에서 상한이 0.98m 라
    # 헬멧이 잘렸다 (docs/measurements/2026-09-13-camera-fov.md 의 권고와 일치).
    # ⚠️ 실기 장착 후 실측값으로 갱신할 것 — 아래는 목표 명목값.
    cam_tilt_deg: float = 15.0  # 위를 본다
    # ⚠️ 확정값 74°/59° 다 (docs/measurements/2026-09-13-camera-fov.md).
    # 이전 32°/48° 는 테이프 마커 배정 오류에서 나온 값 — 그대로 두면 합성
    # 영상이 실제보다 ~2.4배 확대돼 나온다.
    cam_fov_h_deg: float = 74.0
    cam_fov_v_deg: float = 59.0
    cam_resolution: tuple = (640, 480)
    # 본체 앞쪽에 달려 있다 — 머리 전면 밖으로 살짝 돌출된 위치
    cam_forward_offset_m: float = 0.125

    # ── 보행 속도 모형 — 선형 스케일 (실측 기반) ───────────
    # MOVE 의 step 은 보폭(mm). 실측: step 60 에서 전진 104mm/s · 후진 78mm/s.
    fwd_mm_per_sec_per_step: float = 104.0 / 60.0  # ≈1.73 mm/s per mm-step
    rev_mm_per_sec_per_step: float = 78.0 / 60.0  # ≈1.30 — 후진은 75%
    # angle 은 회전 속도 명령(도 단위). 명목 25°/s @ angle 20 — gait_calibration
    # 실측 전까지 명목값, 실측 후 갱신 표시 (config avoidance.nominal 과 동일 근거)
    turn_deg_per_sec_per_angle: float = 25.0 / 20.0  # ≈1.25 °/s per angle-unit
    angle_max_deg: float = 30.0

    # ── 안전 거동 — 실기 프로토콜과 동일하게 흉내낸다 ──────
    cmd_watchdog_ms: int = 300  # 무응답 시 자동 정지 (config 명령 타임아웃)
    telemetry_hz: int = 10


SPEC = MechDogSpec()
