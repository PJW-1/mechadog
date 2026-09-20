"""Run the SDK's actual header parser against every packaged factory voice.

Requires the local vendor SDK and CI1302 image; generated fixtures stay local.
The ROM strncmp pointer is bound to host strncmp; playback hardware is not tested.
"""
import hashlib
import json
import os
import struct
import subprocess
from pathlib import Path

SDK = Path("<WE_SDK_TREE_DIAG>/offline-speaker-1.12.16")
ROOT = Path("<WE_SDK_TREE_MP3>")
OUT = ROOT / "header-regression"
OUT.mkdir(parents=True, exist_ok=True)
PLAYER = SDK / "components/player/audio_play"
raw = Path("<OUT_DIR>/35-diag.bin").read_bytes()
voice_start, voice_size = struct.unpack_from("<II", raw, 8192 + 166 + 4 * 17 + 4)
voice = raw[voice_start:voice_start + voice_size]
count = struct.unpack_from("<H", voice)[0]
rows = []
for index in range(count):
    ident, offset, size = struct.unpack_from("<HII", voice, 2 + index * 10)
    assert offset + size <= len(voice)
    data = voice[offset:offset + size]
    is_mp3 = data[:3] == b"ID3" and data[20:22] == b"CI"
    if is_mp3:
        assert struct.unpack_from("<I", data, 22)[0] == size
    rows.append((ident, size, is_mp3, data[:64].ljust(64, b"\0")))
assert sum(r[2] for r in rows) == 156

body = (PLAYER / "get_play_data.c").read_text(encoding="utf-8")
start = body.index("int32_t check_ci_voice_head(")
brace = body.index("{", start)
depth = 1
end = brace + 1
while depth:
    depth += (body[end] == "{") - (body[end] == "}")
    end += 1
function = body[start:end]
types = (PLAYER / "audio_play_decoder.h").read_text(encoding="utf-8")
types = types[types.index("typedef enum"):types.index("} prompt_decoder_config;") + len("} prompt_decoder_config;")]
fixture = "\n".join("{%d,%d,%d,{%s}}," % (ident, size, is_mp3, ",".join(str(b) for b in head)) for ident, size, is_mp3, head in rows)
harness = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define CHECK(x) do { if(!(x)) { fprintf(stderr,"check failed line %d: %s\n",__LINE__,#x); return 1; } } while(0)
#include "ci_voice_head.h"
#define AUDIO_PLAY_SUPPT_FLAC_PROMPT 0
#define AUDIO_PLAY_SUPPT_IMAADPCM_PROMPT 1
#define ci_logerr(...) ((void)0)
struct rom_stub { struct { int (*strncmp_p)(const char*,const char*,size_t); } newlibcfunc; };
static const struct rom_stub host_rom = {{strncmp}};
#define MASK_ROM_LIB_FUNC (&host_rom)
'''
harness += types + "\n" + function + "\n"
harness += "struct fixture {unsigned id, size, mp3; uint8_t head[64];};\nstatic const struct fixture fixtures[]={\n" + fixture + "\n};\n"
harness += r'''
int main(int argc, char **argv) {
    if (argc>1) CHECK(strcmp(argv[1],"force-failure")!=0);
    unsigned accepted=0, rejected=0;
    for (unsigned i=0;i<sizeof(fixtures)/sizeof(fixtures[0]);++i) {
        ci_voice_head_t head={0};
        memcpy(&head,fixtures[i].head,sizeof(head));
        prompt_decoder_config cfg={0};
        uint32_t bytes=0, skip=0, threshold=0;
        int rc=check_ci_voice_head(&head,&bytes,&skip,&cfg,&threshold);
        if (AUDIO_PLAY_SUPPT_MP3_PROMPT && fixtures[i].mp3) {
            assert(rc==0 && cfg.mode==CI_ADPCM_DECODER_MODE_MP3);
            assert(skip==30 && bytes+skip==fixtures[i].size);
            assert(threshold==head.wave_ci_mp3.pcm_size*2);
            ++accepted;
            head.wave_ci_mp3.CI[0]='X';
            assert(check_ci_voice_head(&head,&bytes,&skip,&cfg,&threshold)==-1);
        } else { assert(rc==-1); ++rejected; }
    }
    /* An existing 48-byte CI PCM envelope must still select the WAV decoder. */
    ci_voice_head_t pcm={0}; prompt_decoder_config cfg={0};
    uint32_t bytes=0, skip=0, threshold=0;
    memcpy(pcm.wave_pcm_48.wfmt.RIFF,"RIFF",4);
    memcpy(pcm.wave_pcm_48.wfmt.WAVE,"WAVE",4);
    memcpy(pcm.wave_pcm_48.wfmt.fmt,"fmt ",4);
    pcm.wave_pcm_48.wfmt.fmt_chunk_size=20;
    pcm.wave_pcm_48.wfmt.wav_formatag=1;
    pcm.wave_pcm_48.wfmt.nSamplesPerSec=16000;
    memcpy(pcm.wave_pcm_48.dchunk.data,"data",4);
    pcm.wave_pcm_48.dchunk.chunksize=32000;
    assert(check_ci_voice_head(&pcm,&bytes,&skip,&cfg,&threshold)==0);
    assert(cfg.mode==CI_ADPCM_DECODER_MODE_WAV && skip==48 && bytes==32000);
    printf("mp3_enabled=%d accepted=%u rejected=%u PCM48=PASS invalid_CI=PASS\n",AUDIO_PLAY_SUPPT_MP3_PROMPT,accepted,rejected);
    return 0;
}
'''
harness = harness.replace("assert(", "CHECK(")
(OUT / "parser_regression.c").write_text(harness, encoding="utf-8")
zig = Path("<ZIG_EXE>")
env = os.environ.copy()
env["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / "zig-local-cache")
env["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / "zig-global-cache")
results = []
for enabled in (0, 1):
    exe = OUT / f"parser_mp3_{enabled}.exe"
    proc = subprocess.run([str(zig), "cc", "-std=c11", "-Wall", "-Wextra", "-O1", f"-DAUDIO_PLAY_SUPPT_MP3_PROMPT={enabled}", "-I", str(PLAYER), str(OUT / "parser_regression.c"), "-o", str(exe)], capture_output=True, env=env, timeout=90)
    (OUT / f"compile-{enabled}.log").write_bytes(proc.stdout + proc.stderr)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors="replace"))
    negative = subprocess.run([str(exe), "force-failure"], capture_output=True, timeout=30)
    assert negative.returncode == 1 and b"check failed" in negative.stderr
    run = subprocess.run([str(exe)], capture_output=True, check=True, timeout=30)
    results.append(run.stdout.decode().strip())
report = {"actual_sdk_function_sha256": hashlib.sha256(function.encode()).hexdigest(), "image_sha256": hashlib.sha256(raw).hexdigest(), "factory_mp3_voices": 156, "metadata_entries": 1, "runs": results, "checks_always_enabled": True, "harness_negative_control": True, "physical_playback_tested": False}
(ROOT / "header-regression.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
