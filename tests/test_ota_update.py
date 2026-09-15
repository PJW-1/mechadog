"""Host preflight prevents unreviewed images and credentials sent to wrong TLS peers."""

import hashlib
import json
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.ota_update import SLOT_SIZE, RobotOta, load_package


def write_package(tmp_path, **changes):
    image = bytearray(256)
    image[0] = 0xE9
    image[2:4] = bytes((2, 32))
    data = bytes(image) + hashlib.sha256(image).digest()
    app = tmp_path / "app.bin"
    app.write_bytes(data)
    package = {
        "application": str(app),
        "application_sha256": hashlib.sha256(data).hexdigest(),
        "image_sha256": data[-32:].hex(),
        "actuators_enabled": False,
        "actuator_off_elf_reviewed": True,
        "ota_protocol": 1,
        "mac": "robot",
    }
    package.update(changes)
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps(package))
    return manifest, app


def test_reviewed_package(tmp_path):
    manifest, app = write_package(tmp_path)
    assert load_package(manifest)[1] == app.read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("actuators_enabled", True),
        ("actuator_off_elf_reviewed", False),
        ("ota_protocol", 2),
        ("application_sha256", "wrong"),
        ("image_sha256", "wrong"),
    ],
)
def test_invalid_metadata_rejected(tmp_path, field, value):
    manifest, _ = write_package(tmp_path, **{field: value})
    with pytest.raises(ValueError):
        load_package(manifest)


@pytest.mark.parametrize("size", [0, 255, SLOT_SIZE + 1])
def test_invalid_length(tmp_path, size):
    manifest, app = write_package(tmp_path)
    app.write_bytes(bytes(size))
    with pytest.raises(ValueError):
        load_package(manifest)


def test_changed_file_rejected(tmp_path):
    manifest, app = write_package(tmp_path)
    raw = bytearray(app.read_bytes())
    raw[70] ^= 1
    app.write_bytes(raw)
    with pytest.raises(ValueError):
        load_package(manifest)


def test_wrong_tls_peer_never_receives_token():
    client = RobotOta.__new__(RobotOta)
    client.config = {"host": "127.0.0.1", "token": "secret", "certificate_sha256": "wrong"}
    client.context = MagicMock()
    with patch("http.client.HTTPSConnection") as cls:
        connection = cls.return_value
        connection.sock.getpeercert.return_value = b"wrong certificate"
        with pytest.raises(ssl.SSLError):
            client.request("GET", "/status")
        connection.request.assert_not_called()
        connection.close.assert_called_once()


def test_wrong_robot_rejected():
    client = RobotOta.__new__(RobotOta)
    client.config = {"mac": "expected"}
    client.request = MagicMock(return_value={"mac": "other", "actuators": False})
    with pytest.raises(ValueError):
        client.status()


def test_large_upload_reads_early_rejection_without_sending_body():
    client = RobotOta.__new__(RobotOta)
    certificate = b"trusted certificate"
    client.config = {
        "host": "127.0.0.1",
        "token": "secret",
        "certificate_sha256": hashlib.sha256(certificate).hexdigest(),
    }
    client.context = MagicMock()
    with patch("http.client.HTTPSConnection") as cls:
        connection = cls.return_value
        connection.sock.getpeercert.return_value = certificate
        connection.sock.pending.return_value = 1
        response = connection.getresponse.return_value
        response.status = 409
        response.read.return_value = b'{"error":"not_ready"}'
        with pytest.raises(RuntimeError, match="HTTP 409"):
            client.request("POST", "/firmware", bytes(8192))
        connection.send.assert_not_called()
        connection.close.assert_called_once()


def test_large_upload_sends_all_chunks():
    client = RobotOta.__new__(RobotOta)
    certificate = b"trusted certificate"
    client.config = {
        "host": "127.0.0.1",
        "token": "secret",
        "certificate_sha256": hashlib.sha256(certificate).hexdigest(),
    }
    client.context = MagicMock()
    with (
        patch("http.client.HTTPSConnection") as cls,
        patch("select.select", return_value=([], [], [])),
    ):
        connection = cls.return_value
        connection.sock.getpeercert.return_value = certificate
        connection.sock.pending.return_value = 0
        response = connection.getresponse.return_value
        response.status = 202
        response.read.return_value = b'{"staged":true}'
        body = bytes(8200)
        assert client.request("POST", "/firmware", body)["staged"]
        assert b"".join(call.args[0] for call in connection.send.call_args_list) == body


def test_transient_confirmation_does_not_finish_before_final_restart():
    client = RobotOta.__new__(RobotOta)
    clock = [0.0]

    def status():
        # Brief confirmed pre-restart image must not be accepted.
        if clock[0] < 1:
            return {"boot": "first", "healthy": True, "confirmed": True}
        return {"boot": "final", "healthy": True, "confirmed": True}

    client.status = status
    with (
        patch("time.monotonic", side_effect=lambda: clock[0]),
        patch("time.sleep", side_effect=lambda dt: clock.__setitem__(0, clock[0] + dt)),
    ):
        assert client.wait_ready(timeout=10)["boot"] == "final"
    assert clock[0] >= 3


def test_update_blocks_unhealthy_robot(tmp_path):
    manifest, _ = write_package(tmp_path)
    client = RobotOta.__new__(RobotOta)
    client.status = MagicMock(return_value={"healthy": False})
    client.request = MagicMock()
    with pytest.raises(ValueError):
        client.update(manifest)
    client.request.assert_not_called()


def test_already_installed_no_flash(tmp_path):
    manifest, _ = write_package(tmp_path)
    package, _ = load_package(manifest)
    client = RobotOta.__new__(RobotOta)
    client.status = MagicMock(
        return_value={
            "healthy": True,
            "confirmed": True,
            "mac": "robot",
            "image_sha256": package["image_sha256"],
        }
    )
    client.request = MagicMock()
    assert client.update(manifest)["state"] == "ALREADY_INSTALLED"
    client.request.assert_not_called()


def test_partition_layout_preserves_data():
    root = Path(__file__).resolve().parents[1]
    rows = {}
    for line in (root / "firmware_mechdog_motion/ota_partitions.csv").read_text().splitlines():
        if line and not line.startswith("#"):
            name, typ, sub, offset, size = line.split(",")
            rows[name] = (typ, sub, int(offset, 0), int(size, 0))
    assert rows["nvs"] == ("data", "nvs", 0x9000, 0x6000)
    assert rows["phy_init"] == ("data", "phy", 0xF000, 0x1000)
    assert rows["vfs"] == ("data", "fat", 0x200000, 0x200000)
    for name in ("ota_0", "ota_1"):
        assert rows[name][3] == SLOT_SIZE
    regions = sorted((v[2], v[2] + v[3]) for v in rows.values())
    assert all(a[1] <= b[0] for a, b in zip(regions, regions[1:], strict=False))
    assert regions[-1][1] == 0x400000
