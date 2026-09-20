"""Apply audited changes to an isolated SDK only. Never flashes a device."""

import difflib
import hashlib
import json
import shutil
from pathlib import Path

BASE = Path("<SDK_ORIG_TREE>/offline-speaker-1.12.16")
ROOT = Path("<WE_SDK_TREE>")
SDK = ROOT / "offline-speaker-1.12.16"
REPO = Path("<REPO_WORKTREE>")
SOURCE = Path(__file__).resolve().parent
DEST = REPO / "experiments/wonderecho-audio/bridge"
assert SDK.is_dir() and REPO.is_dir() and SDK.resolve() != BASE.resolve()
DEST.mkdir(exist_ok=True)
for f in SOURCE.iterdir():
    if f.is_file() and f.suffix in (".c", ".h", ".py", ".md", ".ps1"):
        shutil.copy2(f, DEST / f.name)

changes = []


def modify(relative, transform):
    old = (BASE / relative).read_bytes()
    text = old.decode("utf-8")
    newline = "\r\n" if b"\r\n" in old else "\n"
    text = text.replace("\r\n", "\n")
    updated = transform(text)
    assert updated != text, relative
    target = SDK / relative
    target.write_bytes(updated.replace("\n", newline).encode("utf-8"))
    changes.extend(
        difflib.unified_diff(
            text.splitlines(True),
            updated.splitlines(True),
            fromfile="a/" + relative,
            tofile="b/" + relative,
        )
    )


def replace_one(text, old, new):
    assert text.count(old) == 1, old[:100]
    return text.replace(old, new)


def config(text):
    start = text.index("/* v24: enable the SDK I2C slave")
    end = text.index("/* v19: keep the host UART", start)
    text = (
        text[:start]
        + (
            "/* The external 0x34 bridge connects to UART1, not native IIC0. */\n"
            "#define USE_IIC_PAD 0\n#define MSG_USE_I2C_EN 0\n"
            "#define WE_PROMPT_RECOVERY_EXPERIMENT 0\n"
        )
        + text[end:]
    )
    text = text.replace(
        "/* v15 probe: play prompts through codec 0 = IIS0 on PA2..PA6 pads\n"
        "   (the on-board speaker amp is a digital I2S part per Hiwonder docs). */",
        "/* Verified analog codec-1 speaker route. UART1 owns PA2/PA3. */",
    )
    # Diagnostic build: explicitly keep UART0 logging and the existing WEC1 stub.
    assert "#define CONFIG_CI_LOG_UART HAL_UART0_BASE" in text
    return text


modify("projects/offline_asr_sample/src/user_config.h", config)


def hooks(text):
    text = '#include "we_bridge.h"\n' + text
    key = "__WEAK void sys_asr_result_hook(cmd_handle_t cmd_handle, uint8_t asr_score)\n{"
    return replace_one(
        text, key, key + "\n    we_bridge_asr_result(cmd_info_get_command_id(cmd_handle));"
    )


modify("projects/offline_asr_sample/src/system_hook.c", hooks)


def init(text):
    text = '#include "we_bridge.h"\n' + text
    text = replace_one(
        text,
        "static void prompt_asr_watchdog_cb(TimerHandle_t xTimer)",
        "#if WE_PROMPT_RECOVERY_EXPERIMENT\nstatic void prompt_asr_watchdog_cb(TimerHandle_t xTimer)",
    )
    text = replace_one(
        text, "    prompt_asr_release_stuck();\n}", "    prompt_asr_release_stuck();\n}\n#endif"
    )
    text = replace_one(
        text,
        "    {\n        static TimerHandle_t prompt_asr_watchdog;",
        "    /* Do not run the unverified IDLE-based queue reset in this candidate. */\n"
        "    #if WE_PROMPT_RECOVERY_EXPERIMENT\n    {\n        static TimerHandle_t prompt_asr_watchdog;",
    )
    return replace_one(
        text,
        "    ///tag-gpio-init",
        "    #endif\n    int bridge_result = we_bridge_init();\n"
        '    mprintf("[WE-BRIDGE] init_result=%d (build 3401)\\n", bridge_result);\n'
        "    ///tag-gpio-init",
    )


modify("projects/offline_asr_sample/src/user_msg_deal.c", init)


def limit_gain(text):
    return replace_one(
        text,
        "void audio_play_set_vol_gain(int32_t gain)\n{",
        "void audio_play_set_vol_gain(int32_t gain)\n{\n"
        "    /* User-requested normal volume; do not rewrite persistent NV. */\n"
        "    if (gain > 40) gain = 40;\n    if (gain < 0) gain = 0;",
    )


modify("components/player/audio_play/audio_play_device.c", limit_gain)

for name in ("we_bridge.c", "we_bridge.h", "we_bridge_protocol.c", "we_bridge_protocol.h"):
    shutil.copy2(SOURCE / name, SDK / "projects/offline_asr_sample/src" / name)
modify(
    "projects/offline_asr_sample/project_file/source_file.prj",
    lambda s: (
        replace_one(
            s,
            "source-file: projects/offline_asr_sample/src/voice_prompt.c",
            "// UART0 diagnostic build: voice_stream supplies trace counters; no baked PCM prompt.",
        )
        + "\nsource-file: projects/offline_asr_sample/src/we_bridge.c\n"
        "source-file: projects/offline_asr_sample/src/we_bridge_protocol.c\n"
    ),
)
modify(
    "projects/offline_asr_sample/project_file/strict_stream.mk",
    lambda s: (
        s + "\nbuild/objs/we_bridge.o: C_FLAGS += -Wall -Wextra -Werror\n"
        "build/objs/we_bridge_protocol.o: C_FLAGS += -Wall -Wextra -Werror\n"
    ),
)
modify(
    "projects/offline_asr_sample/project_file/Makefile",
    lambda s: replace_one(
        s,
        "LD_FLAGS += -Wl,-u,we_encoder_init,-u,we_encoder_close,-u,we_encode_frame",
        "# UART0 diagnostic build: do not force-retain the inactive Speex streaming encoder.",
    ),
)
(DEST / "sdk-integration.patch").write_text("".join(changes), encoding="utf-8")

robot = (ROOT / "voice_dispatch.before.cpp").read_text(encoding="utf-8")
fixed = robot.replace('case 2: return "TURN-LEFT";', 'case 3: return "TURN-LEFT";')
fixed = fixed.replace('case 3: return "TURN-RIGHT";', 'case 4: return "TURN-RIGHT";')
fixed = fixed.replace('case 4: return "GO-BACKWARD";', 'case 2: return "GO-BACKWARD";')
fixed = fixed.replace("case 2:  // TURN-LEFT", "case 3:  // TURN-LEFT")
fixed = fixed.replace("case 3:  // TURN-RIGHT", "case 4:  // TURN-RIGHT")
fixed = fixed.replace("case 4:  // GO-BACKWARD", "case 2:  // GO-BACKWARD")
fixed = fixed.replace('case 29: return "DIVE-FORWARD";', 'case 29: return "MARCH";')
assert fixed != robot
(ROOT / "voice_dispatch.corrected.cpp").write_text(fixed, encoding="utf-8")
(DEST / "robot-mapping.patch").write_text(
    "".join(
        difflib.unified_diff(
            robot.splitlines(True),
            fixed.splitlines(True),
            fromfile="a/firmware_mechdog_motion/src/voice_dispatch.cpp",
            tofile="b/firmware_mechdog_motion/src/voice_dispatch.cpp",
        )
    ),
    encoding="utf-8",
)
manifest = {
    f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in DEST.iterdir() if f.is_file()
}
(ROOT / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print("Prepared isolated bridge build and source patches; no device writes.")
