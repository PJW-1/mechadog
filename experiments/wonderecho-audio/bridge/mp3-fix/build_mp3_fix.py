"""Enable vendor MP3 prompt support in a separate v36 SDK copy; never flash."""
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
BASE = Path("<WE_SDK_TREE_DIAG>/offline-speaker-1.12.16")
ROOT = Path("<WE_SDK_TREE_MP3>")
SDK = ROOT / "offline-speaker-1.12.16"
PROJECT = SDK / "projects/offline_asr_sample/project_file"
REPO = Path("<REPO_WORKTREE>/experiments/wonderecho-audio/bridge/mp3-fix")
TOOLCHAIN = Path("<GCC_TOOLCHAIN_BIN>")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


assert (ROOT / "header-regression.json").is_file(), "Run factory header regression first"
header_check = json.loads((ROOT / "header-regression.json").read_text())
assert header_check.get("checks_always_enabled") and header_check.get("harness_negative_control")
baseline_files = [BASE / "projects/offline_asr_sample/src" / n for n in ("user_config.h", "user_msg_deal.c", "we_bridge.c")]
baseline_files += [Path("<OUT_DIR>") / n for n in ("35-diag.bin", "34-bridge.bin", "27.bin", "latest.bin")]
baseline = {str(p): sha(p) for p in baseline_files}
if not SDK.exists():
    shutil.copytree(BASE, SDK, ignore=shutil.ignore_patterns("build", ".git", "__pycache__"))
(ROOT / "v35-baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
changes = []


def modify(relative, transform):
    old = (BASE / relative).read_text(encoding="utf-8")
    updated = transform(old)
    assert updated != old
    (SDK / relative).write_text(updated, encoding="utf-8", newline="\n")
    changes.extend(difflib.unified_diff(old.splitlines(True), updated.splitlines(True), fromfile="a/" + relative, tofile="b/" + relative))


def config(text):
    for flag in ("USE_MP3_DECODER", "AUDIO_PLAY_SUPPT_MP3_PROMPT"):
        text, count = re.subn(r"(?m)^#define " + flag + r"\s+0\b", "#define " + flag + " 1", text)
        assert count == 1, flag
    return text


modify("projects/offline_asr_sample/src/user_config.h", config)
modify("projects/offline_asr_sample/src/we_bridge.c", lambda t: t.replace("build=3501", "build=3601"))
modify("projects/offline_asr_sample/src/user_msg_deal.c", lambda t: t.replace("build 3501", "build 3601"))
(SOURCE / "v36-from-v35.patch").write_text("".join(changes), encoding="utf-8")
env = os.environ.copy()
env["PATH"] = str(TOOLCHAIN) + ";" + str(SDK / "tools/build-tools/bin") + ";" + env["PATH"]
(PROJECT / "build").mkdir(exist_ok=True)
with (ROOT / "firmware-build.log").open("wb") as output:
    built = subprocess.run([str(SDK / "tools/build-tools/bin/make.exe"), "PROJECT_NAME=offline-stream", "-f", "makefile", "-f", "strict_stream.mk", "-j6", "build/offline-stream.elf"], cwd=PROJECT, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=240)
if built.returncode:
    print((ROOT / "firmware-build.log").read_text(encoding="utf-8", errors="replace")[-6000:])
    raise SystemExit(built.returncode)

packer = (SOURCE.parent / "wonderecho-bridge-work/package_bridge.py").read_text(encoding="utf-8")
packer = packer.replace("voicebridge-sdk-20260919", "voicebridge-mp3-20260919").replace("3401", "3601").replace("2.1.34", "2.1.36").replace("34-bridge.bin", "36-mp3.bin")
(SOURCE / "package_mp3_fix.py").write_text(packer, encoding="utf-8")
subprocess.run([sys.executable, str(SOURCE / "package_mp3_fix.py")], check=True, timeout=90)
for name, digest in baseline.items():
    assert sha(Path(name)) == digest, name
changed = []
for original in BASE.rglob("*"):
    if not original.is_file():
        continue
    relative = original.relative_to(BASE)
    if any(part in ("build", ".git", "__pycache__") for part in relative.parts):
        continue
    if sha(original) != sha(SDK / relative):
        changed.append(relative.as_posix())
expected = {"projects/offline_asr_sample/src/" + n for n in ("user_config.h", "we_bridge.c", "user_msg_deal.c")}
assert set(changed) == expected, changed
result = {"build": 3601, "compile": "passed", "changed_sdk_files": changed, "functional_change": "Enable existing MP3 decoder and CI MP3 prompt header support", "other_changes": "Build identifier only", "v35_v34_v27_latest_preserved": True, "volume_cap": 40, "installed": False, "physical_playback_verified": False}
(ROOT / "fix-verification.json").write_text(json.dumps(result, indent=2) + "\n")
REPO.mkdir(parents=True, exist_ok=True)
for file in SOURCE.iterdir():
    if file.is_file() and file.suffix in (".py", ".patch", ".md"):
        shutil.copy2(file, REPO / file.name)
print(json.dumps(result, indent=2))
