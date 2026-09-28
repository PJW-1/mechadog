"""Build v35 observations in a new SDK copy. No COM, network, or device writes."""

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
BASE = Path("<WE_SDK_TREE>/offline-speaker-1.12.16")
ROOT = Path("<WE_SDK_TREE_DIAG>")
SDK = ROOT / "offline-speaker-1.12.16"
PROJECT = SDK / "projects/offline_asr_sample/project_file"
REPO = Path("<REPO_WORKTREE>/experiments/wonderecho-audio/bridge/diagnostics")
TOOLCHAIN = Path("<GCC_TOOLCHAIN_BIN>")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def once(text, old, new):
    assert text.count(old) == 1, old
    return text.replace(old, new)


baseline_files = [
    BASE / "projects/offline_asr_sample/src" / name
    for name in (
        "main.c",
        "user_msg_deal.c",
        "user_config.h",
        "we_bridge.c",
        "we_bridge.h",
        "we_bridge_protocol.c",
        "we_bridge_protocol.h",
    )
]
baseline_files += [Path("<OUT_DIR>") / name for name in ("34-bridge.bin", "27.bin", "latest.bin")]
baseline = {str(p): sha(p) for p in baseline_files}
if not SDK.exists():
    shutil.copytree(BASE, SDK, ignore=shutil.ignore_patterns("build", ".git", "__pycache__"))
ROOT.mkdir(exist_ok=True)
(ROOT / "v34-baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
changes = []


def write_changed(relative, updated):
    old = (BASE / relative).read_text(encoding="utf-8")
    (SDK / relative).write_text(updated, encoding="utf-8", newline="\n")
    changes.extend(
        difflib.unified_diff(
            old.splitlines(True),
            updated.splitlines(True),
            fromfile="a/" + relative,
            tofile="b/" + relative,
        )
    )


for name in ("we_bridge.c", "we_bridge.h"):
    write_changed(
        "projects/offline_asr_sample/src/" + name, (SOURCE / name).read_text(encoding="utf-8")
    )
for name in ("we_bridge_protocol.c", "we_bridge_protocol.h"):
    assert sha(SOURCE / name) == sha(BASE / "projects/offline_asr_sample/src" / name)

rel = "projects/offline_asr_sample/src/main.c"
text = (BASE / rel).read_text(encoding="utf-8")
text = '#include "we_bridge.h"\n' + text
text = once(
    text,
    "        vPortFree(StatusArray);",
    "        vPortFree(StatusArray);\n        we_bridge_log_status(); /* RAM-only, once per existing 10-second interval. */",
)
write_changed(rel, text)

rel = "projects/offline_asr_sample/src/user_msg_deal.c"
text = (BASE / rel).read_text(encoding="utf-8")
write_changed(rel, once(text, "(build 3401)", "(build 3501)"))
(SOURCE / "v35-from-v34.patch").write_text("".join(changes), encoding="utf-8")

env = os.environ.copy()
env["PATH"] = str(TOOLCHAIN) + ";" + str(SDK / "tools/build-tools/bin") + ";" + env["PATH"]
(PROJECT / "build").mkdir(exist_ok=True)
with (ROOT / "firmware-build.log").open("wb") as output:
    result = subprocess.run(
        [
            str(SDK / "tools/build-tools/bin/make.exe"),
            "PROJECT_NAME=offline-stream",
            "-f",
            "makefile",
            "-f",
            "strict_stream.mk",
            "-j6",
            "build/offline-stream.elf",
        ],
        cwd=PROJECT,
        env=env,
        stdout=output,
        stderr=subprocess.STDOUT,
        timeout=240,
    )
if result.returncode:
    print((ROOT / "firmware-build.log").read_text(encoding="utf-8", errors="replace")[-6000:])
    raise SystemExit(result.returncode)

# Preserve the already verified official packer workflow; change only candidate identity.
packer = (SOURCE.parent / "wonderecho-bridge-work/package_bridge.py").read_text(encoding="utf-8")
packer = (
    packer.replace("voicebridge-sdk-20260919", "voicebridge-diag-20260919")
    .replace("3401", "3501")
    .replace("2.1.34", "2.1.35")
    .replace("34-bridge.bin", "35-diag.bin")
)
(SOURCE / "package_diagnostics.py").write_text(packer, encoding="utf-8")
subprocess.run([sys.executable, str(SOURCE / "package_diagnostics.py")], check=True, timeout=90)
for path, digest in baseline.items():
    assert sha(Path(path)) == digest, "Baseline changed: " + path

# Permit only the four diagnostic source edits, apart from generated build output.
changed = []
for original in BASE.rglob("*"):
    if not original.is_file():
        continue
    relpath = original.relative_to(BASE)
    if any(part in ("build", ".git", "__pycache__") for part in relpath.parts):
        continue
    if sha(original) != sha(SDK / relpath):
        changed.append(relpath.as_posix())
expected = {
    "projects/offline_asr_sample/src/" + n
    for n in ("main.c", "user_msg_deal.c", "we_bridge.c", "we_bridge.h")
}
assert set(changed) == expected, changed
verification = {
    "build": 3501,
    "compile": "passed",
    "changed_sdk_files": changed,
    "v34_and_latest_preserved": True,
    "protocol_unchanged": True,
    "volume_cap_unchanged": 40,
    "device_installed": False,
    "acoustic_success": None,
    "runtime_verified": False,
}
(ROOT / "diagnostic-verification.json").write_text(json.dumps(verification, indent=2) + "\n")
REPO.mkdir(parents=True, exist_ok=True)
for path in SOURCE.iterdir():
    if path.is_file() and path.suffix in (".c", ".h", ".py", ".md", ".patch"):
        shutil.copy2(path, REPO / path.name)
print(json.dumps(verification, indent=2))
