"""Share the USB utility's operation lock with GUI wireless updates."""

import sys
from pathlib import Path

from mode_control import exclusive_operation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ota_update import main

if __name__ == "__main__":
    with exclusive_operation():
        raise SystemExit(main())
