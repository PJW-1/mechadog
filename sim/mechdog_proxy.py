"""MechDog 프록시 — 실측 치수의 USD 형상 + 키네마틱 구동.

몸통·머리·다리를 원시 형상(primitive)으로 조립한다. 벤더가 URDF 를 제공하지
않으므로 **치수·질량·카메라 포즈가 맞는 프록시**를 직접 만든다 — 동선·시야
데이터에는 형상 정밀도보다 물리 치수 정확도가 중요하다.

구동은 `MOVE(step, angle)` 의 호(arc) 조향을 그대로 모사한다 — 양수 angle 은
CCW(좌회전). 제자리 회전 없음(DR-11). 300ms 명령 워치독·ESTOP 래치도
실기와 같은 규칙으로 흉내낸다 (`sim/mechdog_spec.py` 참조).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .mechdog_spec import SPEC


@dataclass(slots=True)
class KinematicState:
    """프록시의 2D 자세 — 시뮬 좌표(미터, 라디안)."""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0  # CCW 양수 (PROTOCOL.md 와 동일 부호)
    v_mps: float = 0.0  # 현재 전후 속도
    omega_dps: float = 0.0  # 현재 회전 속도 (deg/s)
    failsafe_latched: bool = False
    last_cmd_ms: int = 0
    #: 호스트가 STATE 로 알려준 FSM 상태 — 로봇은 판단에 쓰지 않고 되돌려준다
    # (tools/mock_mechdog.py 의 host_state 와 같은 의미)
    host_state: str = ""

    def apply(self, kind: str, now_ms: int, **fields) -> None:
        """실기 프로토콜 의미를 그대로 적용한다 (PROTOCOL.md 의 안전 계약)."""
        self.last_cmd_ms = now_ms
        if kind == "ESTOP":
            self.failsafe_latched = True
            self.v_mps = self.omega_dps = 0.0
        elif kind == "RESET_SAFE":
            self.failsafe_latched = False
            self.v_mps = self.omega_dps = 0.0
        elif kind == "STOP":
            self.v_mps = self.omega_dps = 0.0
        elif kind == "STATE":
            # 호스트 FSM 이 알려주는 현재 상태 — 텔레메트리로 되돌려준다.
            self.host_state = str(fields.get("state", ""))
        elif kind == "MOVE" and not self.failsafe_latched:
            step = float(fields.get("step", 0.0))
            angle = float(fields.get("angle", 0.0))
            rate = SPEC.fwd_mm_per_sec_per_step if step >= 0 else SPEC.rev_mm_per_sec_per_step
            self.v_mps = step * rate / 1000.0
            self.omega_dps = angle * SPEC.turn_deg_per_sec_per_angle

    def step(self, dt_s: float, now_ms: int) -> None:
        """dt 만큼 호 궤적을 적분한다. 워치독이 끊으면 멈춘다 — 실기와 같다."""
        if self.failsafe_latched or (now_ms - self.last_cmd_ms) > SPEC.cmd_watchdog_ms:
            self.v_mps = self.omega_dps = 0.0
        omega = math.radians(self.omega_dps)
        if abs(omega) > 1e-9:
            # 호 궤적 — 직선 근사 대신 정확한 원호 적분
            radius = self.v_mps / omega
            self.x += radius * (math.sin(self.yaw + omega * dt_s) - math.sin(self.yaw))
            self.y -= radius * (math.cos(self.yaw + omega * dt_s) - math.cos(self.yaw))
            self.yaw += omega * dt_s
        else:
            self.x += self.v_mps * dt_s * math.cos(self.yaw)
            self.y += self.v_mps * dt_s * math.sin(self.yaw)


def build_robot(stage, prim_path: str = "/World/MechDog") -> str:
    """실측 치수의 프록시 형상을 씬에 만든다. 루트 prim 경로를 돌려준다.

    색: 본체 진회색(알루미늄), 머리 검정, 눈 LED 청록 — 육안 구분용이며
    실기 외관에 맞춘다. 다리는 어깨·정강이 2절 — 치수 비율만 맞춘다.
    """
    from pxr import Gf, Sdf, UsdGeom, UsdPhysics, UsdShade  # noqa: N806

    length = SPEC.body_length_m
    width = SPEC.body_width_m
    height = SPEC.body_height_m

    def _cube(path, size_xyz, pos, rgb):
        cube = UsdGeom.Cube.Define(stage, f"{prim_path}/{path}")
        cube.GetSizeAttr().Set(1.0)
        xf = UsdGeom.Xformable(cube.GetPrim())
        xf.AddScaleOp().Set(Gf.Vec3f(*size_xyz))
        xf.AddTranslateOp().Set(Gf.Vec3d(*pos))
        mat = UsdShade.Material.Define(stage, f"{prim_path}/Looks/{path}_mat")
        shader = UsdShade.Shader.Define(stage, f"{prim_path}/Looks/{path}_shader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        UsdShade.MaterialBindingAPI(cube.GetPrim()).Bind(mat)
        return cube

    stage.DefinePrim(prim_path, "Xform")

    # 본체 — 알루미늄 섀시 상자. 높이는 다리 길이(0.09m) 위에 얹힌다.
    leg_len = 0.09
    body_z = leg_len + height * 0.5
    _cube("body", (length * 0.75, width * 0.8, height * 0.35), (0, 0, body_z), (0.18, 0.18, 0.20))
    # 머리 — 전방 상단
    _cube(
        "head",
        (length * 0.22, width * 0.55, height * 0.30),
        (length * 0.42, 0, body_z + height * 0.28),
        (0.05, 0.05, 0.06),
    )
    # 눈 LED — 머리 전면의 청록 띠
    _cube(
        "eye_led",
        (0.005, width * 0.4, height * 0.08),
        (length * 0.42 + length * 0.11 + 0.001, 0, body_z + height * 0.30),
        (0.0, 0.8, 0.9),
    )
    # 다리 4조 — 어깨(옆으로)+정강이(아래로). 치수 비율만 맞춘다.
    for name, sx, sy in (("FL", 1, 1), ("FR", 1, -1), ("BL", -1, 1), ("BR", -1, -1)):
        hx = sx * length * 0.32
        hy = sy * width * 0.42
        _cube(
            f"leg_{name}_shoulder",
            (0.022, 0.045, 0.022),
            (hx, hy + sy * 0.018, leg_len * 0.92),
            (0.22, 0.22, 0.24),
        )
        _cube(
            f"leg_{name}_shin",
            (0.018, 0.018, leg_len * 0.9),
            (hx, hy + sy * 0.035, leg_len * 0.5),
            (0.12, 0.12, 0.14),
        )

    # 충돌은 루트에만 — 다리 디테일의 충돌은 데이터에 무의미하다
    body_prim = stage.GetPrimAtPath(f"{prim_path}/body")
    UsdPhysics.CollisionAPI.Apply(body_prim)

    return prim_path


def attach_camera(
    robot_prim_path: str,
    cam_path: str | None = None,
    tilt_deg: float | None = None,
    height_m: float | None = None,
):
    """실측 높이·틸트·화각의 FPV 카메라를 로봇에 단다.

    tilt_deg / height_m 을 주면 SPEC 대신 그 값으로 단다 — 마운트 변경
    실측 전에 후보 각도로 데이터셋을 미리 만들어 볼 때 쓴다.
    """
    import numpy as np
    from isaacsim.sensors.camera import Camera

    if cam_path is None:
        cam_path = f"{robot_prim_path}/cam"

    tilt = SPEC.cam_tilt_deg if tilt_deg is None else tilt_deg
    height = SPEC.cam_height_m if height_m is None else height_m

    cam = Camera(
        prim_path=cam_path,
        resolution=SPEC.cam_resolution,
        translation=np.array(
            [SPEC.cam_forward_offset_m, 0.0, height]
        ),
        orientation=np_tilt_quat(tilt),
    )
    # 화각 → 초점거리/조리개. 수평 조리개 2*f*tan(fov/2) 관계를 이용한다.
    f_mm = 8.0
    h_ap = 2.0 * f_mm * math.tan(math.radians(SPEC.cam_fov_h_deg) / 2.0)
    v_ap = 2.0 * f_mm * math.tan(math.radians(SPEC.cam_fov_v_deg) / 2.0)
    cam.set_focal_length(f_mm / 10.0)  # Camera API 는 cm 단위
    cam.set_horizontal_aperture(h_ap / 10.0)
    cam.set_vertical_aperture(v_ap / 10.0)
    # USD 기본 near-clip 은 1m — 15cm 카메라에선 바로 앞 바닥이 잘려 검게 보인다
    cam.set_clipping_range(0.01, 1000000.0)
    return cam


def attach_overview_camera(robot_prim_path: str, cam_path: str | None = None):
    """로봇을 뒤·위에서 비추는 조망 카메라 — 관제 웹이 시뮬 공장을 보는 눈.

    로봇 prim 의 자식으로 달아 키네마틱이 옮길 때마다 따라간다 (체이스 캠).
    로봇 로컬 좌표로 뒤 1.1m·위 0.75m 에 두고 로봇 중심을 향해 ~30° 내다본다.
    """
    import numpy as np
    from isaacsim.sensors.camera import Camera

    if cam_path is None:
        cam_path = f"{robot_prim_path}/overview_cam"

    cam = Camera(
        prim_path=cam_path,
        resolution=(960, 540),  # 관제 스테이지는 가로가 넓다 — 16:9
        translation=np.array([-1.1, 0.0, 0.75]),
        orientation=np_tilt_quat(-30.0),
    )
    cam.set_clipping_range(0.01, 1000000.0)
    return cam


def np_cam_offset():
    import numpy as np

    # 로봇 로컬 좌표 — 전방 상단에 렌즈. 루트 prim 이 바닥 기준이므로
    # z 는 실측 카메라 높이 그대로다.
    return np.array([SPEC.cam_forward_offset_m, 0.0, SPEC.cam_height_m])


def np_cam_quat():
    """아래로 7° 기운 FPV 카메라 — `np_tilt_quat` 참고."""
    return np_tilt_quat(SPEC.cam_tilt_deg)


def np_tilt_quat(tilt_deg: float):
    """전방에서 tilt 만큼 기운 카메라의 쿼터니언 — 음수면 아래를 본다.

    isaacsim `Camera` 생성자·`set_local_pose` 의 기본 `camera_axes="world"` 는
    로컬 +X 가 시선·+Z 가 위다 (내부에서 USD 축으로 변환한다).
    기저: 시선 f = 전방(+X)에서 tilt 만큼 아래로, 위 u ≈ +Z, 좌 l = u×f.
    외부 라이브러리 없이 회전행렬 → 쿼터니언(wxyz)으로 변환한다.
    """
    import numpy as np

    t = math.radians(-tilt_deg)  # tilt 가 음수면 아래를 본다
    f = np.array([math.cos(t), 0.0, -math.sin(t)])
    u = np.array([math.sin(t), 0.0, math.cos(t)])
    left = np.cross(u, f)  # +Y = 좌측 (우손 좌표계)
    m = np.stack([f, left, u], axis=1)  # 열 기저 회전행렬

    tr = np.trace(m)
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2.0
        w, x, y, z = (
            s / 4.0,
            (m[2, 1] - m[1, 2]) / s,
            (m[0, 2] - m[2, 0]) / s,
            (m[1, 0] - m[0, 1]) / s,
        )
    else:  # 대각 최대 성분 분기
        i = int(np.argmax(np.diag(m)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(m[i, i] - m[j, j] - m[k, k] + 1.0) * 2.0
        q = [0.0, 0.0, 0.0, 0.0]
        q[0] = (m[k, j] - m[j, k]) / s
        q[i + 1] = s / 4.0
        q[j + 1] = (m[j, i] + m[i, j]) / s
        q[k + 1] = (m[k, i] + m[i, k]) / s
        w, x, y, z = q
    return np.array([w, x, y, z])
