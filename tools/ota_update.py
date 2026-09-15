"""Pinned-TLS client for the explicitly provisioned stationary MechDog updater.

No serial resets. Only accepts reviewed package manifests, not arbitrary BINs.
Client credentials and firmware packages stay outside Git.
"""

import argparse
import hashlib
import http.client
import json
import select
import ssl
import time
from pathlib import Path

SLOT_SIZE = 0xF0000


def load_package(path):
    package = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        package.get("actuators_enabled") is not False
        or package.get("actuator_off_elf_reviewed") is not True
        or package.get("ota_protocol") != 1
    ):
        raise ValueError("Package must be a reviewed actuator-OFF OTA image")
    data = Path(package["application"]).read_bytes()
    if not 256 <= len(data) <= SLOT_SIZE:
        raise ValueError("Image does not fit the preserved-data OTA partition")
    if data[0] != 0xE9 or data[2:4] != bytes((2, 32)) or data[12:14] != bytes(2):
        raise ValueError("Expected ESP32 DIO40 / 4MB image")
    if hashlib.sha256(data).hexdigest() != package["application_sha256"]:
        raise ValueError("Package file SHA-256 mismatch")
    if hashlib.sha256(data[:-32]).digest() != data[-32:]:
        raise ValueError("ESP image appended digest mismatch")
    if package["image_sha256"] != data[-32:].hex():
        raise ValueError("Expected runtime image digest mismatch")
    return package, data


class RobotOta:
    def __init__(self, config):
        self.config = config
        self.context = ssl.create_default_context(cafile=config["certificate"])
        # DHCP IP is not a certificate identity; verify CA AND exact certificate.
        self.context.check_hostname = False

    def request(self, method, endpoint, body=None, headers=None):
        connection = http.client.HTTPSConnection(
            self.config["host"], self.config.get("port", 8443), context=self.context, timeout=8
        )
        try:
            connection.connect()
            observed = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if observed != self.config["certificate_sha256"]:
                raise ssl.SSLError("Robot TLS certificate pin mismatch")
            fields = {"Authorization": "Bearer " + self.config["token"], "Connection": "close"}
            fields.update(headers or {})
            if body and len(body) > 4096:
                # Check early rejections while streaming; sendall(whole_image)
                # can block when the server rejects before reading the body.
                fields["Content-Length"] = str(len(body))
                connection.putrequest(method, endpoint)
                for key, value in fields.items():
                    connection.putheader(key, value)
                connection.endheaders()
                deadline = time.monotonic() + 120
                for offset in range(0, len(body), 4096):
                    if (
                        connection.sock.pending()
                        or select.select([connection.sock], [], [], 0.01)[0]
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Firmware transfer deadline exceeded")
                    connection.send(body[offset : offset + 4096])
            else:
                connection.request(method, endpoint, body=body, headers=fields)
            response = connection.getresponse()
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError("Oversized OTA response")
            result = json.loads(raw)
            if response.status >= 400:
                raise RuntimeError(f"Robot rejected request: HTTP {response.status}, {result}")
            return result
        finally:
            connection.close()

    def status(self):
        result = self.request("GET", "/status")
        if result.get("mac") != self.config["mac"] or result.get("actuators") is not False:
            raise ValueError("Wrong device or non-stationary firmware")
        return result

    def wait_ready(self, expected=None, previous_boot=None, timeout=65, confirm=True):
        deadline = time.monotonic() + timeout
        last = None
        last_error = None
        stable_boot = None
        stable_since = None
        while time.monotonic() < deadline:
            try:
                last = self.status()
                last_error = None
                identity_ok = (expected is None or last.get("image_sha256") == expected) and (
                    previous_boot is None or last.get("boot") != previous_boot
                )
                if identity_ok and last.get("healthy"):
                    if last.get("confirmed"):
                        if stable_boot != last.get("boot"):
                            stable_boot = last.get("boot")
                            stable_since = time.monotonic()
                        elif time.monotonic() - stable_since >= 2:
                            # Confirmation itself writes flash and schedules a
                            # final restart; do not report its transient state.
                            return last
                    elif confirm:
                        self.request("POST", "/confirm", body=b"")
                if not identity_ok or not last.get("healthy") or not last.get("confirmed"):
                    stable_boot = None
                    stable_since = None
            except (OSError, http.client.HTTPException, ValueError, RuntimeError) as exc:
                last_error = type(exc).__name__
                stable_boot = None
                stable_since = None
            # Bounded PC worker retry, never part of robot/UI control loops.
            time.sleep(0.6)
        raise TimeoutError(f"No confirmed healthy expected image; last={last}; error={last_error}")

    def update(self, manifest):
        package, data = load_package(manifest)
        before = self.status()
        if not before.get("healthy") or not before.get("confirmed"):
            raise ValueError("Current app must first be healthy and confirmed")
        if package.get("mac") != before["mac"]:
            raise ValueError("Package belongs to another robot")
        if before["image_sha256"] == package["image_sha256"]:
            return {"state": "ALREADY_INSTALLED", "after": before}
        staged = self.request(
            "POST",
            "/firmware",
            body=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Image-SHA256": package["application_sha256"],
            },
        )
        if staged.get("staged") is not True:
            raise RuntimeError("Robot did not acknowledge a staged image")
        after = self.wait_ready(package["image_sha256"], before["boot"])
        return {"state": "UPDATED_AND_CONFIRMED", "before": before, "after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "confirm", "update"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "update" and args.package is None:
        parser.error("update requires --package")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as report:
        try:
            client = RobotOta(json.loads(args.config.read_text(encoding="utf-8")))
            if args.action == "status":
                result = {"state": "STATUS", "status": client.status()}
            elif args.action == "confirm":
                result = {"state": "CONFIRMED", "status": client.wait_ready()}
            else:
                result = client.update(args.package)
        except (OSError, ValueError, KeyError, RuntimeError, http.client.HTTPException) as exc:
            result = {"state": "FAILED", "error": str(exc)}
        json.dump(result, report, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["state"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
