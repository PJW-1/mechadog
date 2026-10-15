"""로봇·센서를 시작하지 않는 흰색 관제 계획 편집 서버.

python -m host.dashboard.preview --device mechdog-02 --port 8003
저장 버튼은 해당 PC의 개체 local.yaml과 maps/zones.json을 실제로 저장한다.
"""

from __future__ import annotations

import argparse

import uvicorn

from host.common.config import load_config
from host.dashboard.planning import PlanningService
from host.dashboard.server import DEFAULT_STATIC_DIR, create_app
from host.dashboard.state import DashboardState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    config = load_config(args.device)
    app = create_app(
        DashboardState(args.device, stale_after_ms=3000),
        static_dir=DEFAULT_STATIC_DIR,
        planning=PlanningService(config, args.device, runtime_attached=False),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
