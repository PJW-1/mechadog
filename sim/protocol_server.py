"""프로토콜 서버 — 시뮬 로봇이 **실기와 같은 선**을 말한다.

`host/common/protocol.py` 의 `CommandDecoder`·`TelemetryEncoder` 를 그대로 쓴다 —
명령 포트(5001)로 JSON 명령을 받고, 텔레메트리(5101)로 10Hz 상태를 쏜다.
대시보드·teleop·behavior 코드는 **IP 만 바꾸면** 시뮬과 실기를 구분하지 못한다.
이것이 "물리 환경 / 시뮬 환경 듀얼"의 의미다 — 위쪽 코드는 그대로고
네트워크 끝만 바뀐다.

UDP 수신은 논블로킹 소켓을 프레임마다 한 번 비우는 방식 — Isaac 스텝 루프를
막지 않는다.
"""

from __future__ import annotations

import contextlib
import math
import socket
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from host.common.protocol import (  # noqa: E402
    CommandDecoder,
    TelemetryEncoder,
)

CMD_PORT = 5001
TELEMETRY_PORT = 5101


class SimProtocolServer:
    """시뮬 로봇의 프로토콜 끝점. 프레임마다 `poll()` 을 부른다."""

    def __init__(self, kinematic, host: str = "0.0.0.0", device_id: str = "mechdog-sim") -> None:
        self.kinematic = kinematic
        self.decoder = CommandDecoder()
        # 시뮬 기체는 이름에 sim 을 붙인다 — 텔레메트리만 봐도 출처가 드러나게
        self.encoder = TelemetryEncoder(device_id=device_id, boot_id=f"sim-{int(time.time())}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, CMD_PORT))
        self.sock.setblocking(False)
        self.peer: tuple | None = None  # 텔레메트리 목적지 — 명령 온 곳
        self._last_telemetry_ms = 0

    def poll(self, now_ms: int | None = None) -> list[dict]:
        """들어온 명령을 전부 소비하고 적용한다. 처리한 결과 목록을 돌려준다."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        handled = []
        while True:
            try:
                raw, addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            # 명령이 온 소켓 그대로 돌려보낸다 — 호스트는 송수신을 같은 포트에 묶는다
            # (open_socket 주석 참고). 포트를 박아 두면 개체 프로파일이
            # telemetry_port 를 바꿨을 때(충돌 회피 등) 답이 엉뚱한 곳으로 간다.
            self.peer = addr
            result = self.decoder.decode(raw)
            # 받아들인 패킷만 적용한다 — 폐기는 링크 갱신도 안 된다 (규칙 ③)
            if not result.accepted or result.message is None:
                continue
            kind = result.message["type"]
            fields = {k: v for k, v in result.message.items() if k not in ("seq", "ts", "type")}
            self.kinematic.apply(kind, now_ms, **fields)
            handled.append({"type": kind, "fields": fields})
        return handled

    def emit_telemetry(
        self,
        now_ms: int | None = None,
        *,
        batt_v: float = 8.0,
        dist_cm: int = 999,
        tipped: bool = False,
    ) -> None:
        """10Hz 로 상태를 쏜다. peer 가 아직 없으면(아무도 명령 안 보냄) 보내지 않는다."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if self.peer is None:
            return
        period_ms = 1000 // 10
        if now_ms - self._last_telemetry_ms < period_ms:
            return
        self._last_telemetry_ms = now_ms
        k = self.kinematic
        # state 는 FSM_STATES 안의 값만 허용된다 (protocol.py). 로봇이 스스로
        # 판정하는 건 FAILSAFE 뿐이고, 나머지는 호스트가 STATE 로 알려준 것을
        # 되돌려준다 — 실기 펌웨어·mock_mechdog 와 같은 규약.
        # ⚠️ imu.yaw 는 도(degree) 0~360 — 인코더가 범위를 강제한다. 키네마틱은
        # 라디안이므로 여기서 변환하고 정규화한다 (그렇지 않으면 회전 후 예외).
        line = self.encoder.encode(
            state="FAILSAFE" if k.failsafe_latched else (k.host_state or "IDLE"),
            dist_cm=dist_cm,
            imu={"yaw": round(math.degrees(k.yaw) % 360, 1), "pitch": 0.0, "roll": 0.0},
            batt_v=batt_v,
            last_cmd_age_ms=now_ms - k.last_cmd_ms,
            flags={
                "lowbatt": False,
                "tipped": tipped,
                "link_ok": True,
                "obstacle": False,
                "sim": True,  # 시뮬 출처 표시
            },
            motion={
                "vx_mps": k.v_mps,
                "omega_dps": k.omega_dps,
                "x": round(k.x, 3),
                "y": round(k.y, 3),
            },
            safety_latched=k.failsafe_latched,
        )
        with contextlib.suppress(OSError):
            self.sock.sendto(line.encode(), self.peer)
