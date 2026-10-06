"""Explicit private settings shared by the Windows desktop utilities."""

import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class Settings:
    def __init__(self, filename=None):
        self.filename = Path(filename).resolve() if filename else None
        self.values = (
            json.loads(self.filename.read_text(encoding="utf-8-sig")) if self.filename else {}
        )
        self.base = self.filename.parent if self.filename else Path.cwd()
        self.data_dir = self.path("data_dir") if self.filename else self.base
        self.port = self.values.get("port", "")
        self.mac = self.values.get("mac", "")
        self.packages = [self.resolve(p) for p in self.values.get("packages", [])]
        self.lock_file = (
            self.path("lock_file")
            if "lock_file" in self.values
            else self.data_dir / "operation.lock"
        )

    def resolve(self, value):
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.base / path).resolve()

    def path(self, key):
        return self.resolve(self.values[key])

    def validate(self):
        if self.filename is None:
            raise ValueError("Set MECHADOG_TOOL_CONFIG to a private settings JSON outside Git")
        for path in (self.filename, self.data_dir, self.lock_file):
            if path.is_relative_to(REPO):
                raise ValueError(
                    "Settings, runtime data and lock must remain outside this repository"
                )
        if not isinstance(self.mac, str) or not re.fullmatch(r"[0-9a-f]{12}", self.mac):
            raise ValueError("mac must be the verified robot's 12 lowercase hex digits")
        if not isinstance(self.port, str) or not re.fullmatch(r"COM[1-9][0-9]*", self.port):
            raise ValueError("port must explicitly identify the robot's Windows COM port")
        if not self.packages:
            raise ValueError("At least one reviewed actuator-OFF package is required")
        return self

    def client(self):
        self.validate()
        config_path = self.path("ota_client")
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if config.get("mac") != self.mac:
            raise ValueError("OTA client identity differs from the selected robot")
        certificate = Path(config["certificate"])
        if not certificate.is_absolute():
            config["certificate"] = str((config_path.parent / certificate).resolve())
        return config


SETTINGS = Settings(os.environ.get("MECHADOG_TOOL_CONFIG"))


def require_settings():
    return SETTINGS.validate()
