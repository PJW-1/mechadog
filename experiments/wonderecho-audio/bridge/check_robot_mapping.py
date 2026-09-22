"""Compile an external vendor dispatcher against bounded fake hardware."""

import argparse
import os
import re
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Manually check an external vendor voice dispatcher; not a repository test."
)
parser.add_argument("--source", type=Path, required=True, help="vendor voice_dispatch.cpp")
parser.add_argument("--zig", type=Path, required=True, help="zig executable")
parser.add_argument("--work-dir", type=Path, required=True, help="directory for generated files")
args = parser.parse_args()

ROOT = args.work_dir
ROOT.mkdir(parents=True, exist_ok=True)
ZIG = args.zig
source = args.source.read_text(encoding="utf-8")
source = re.sub(r"^#include[^\n]*\n", "", source, flags=re.MULTILINE)
prefix = r"""
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cstdint>
#include <cstring>
#include <cstdio>
uint32_t millis() { return 1000; }
struct FakeSerial { template<class... T> void printf(const char*, T...) {} } Serial;
struct FakeWire {
    uint16_t getTimeOut() { return 5; }
    void setTimeOut(uint16_t) {}
    void beginTransmission(uint8_t) {}
    unsigned write(uint8_t) { return 1; }
    uint8_t endTransmission(bool) { return 0; }
    unsigned requestFrom(uint8_t,uint8_t,bool) { return 1; }
    int read() { return 0; }
} Wire;
namespace mechadog {
struct MotionHal {
    float step=0, angle=0; int action_id=-1, moves=0, stops=0;
    void move(float s,float a) { step=s;angle=a;++moves; }
    void stop() { ++stops; }
    void action(int a) { action_id=a; }
};
struct MotionSafetyState {
    bool allowed=true;
    void note_move(float,float) {}
    bool can_action() { return allowed; }
    void stop() {}
};
struct SafetyMonitor {};
bool pending=false;
bool move_allowed(MotionSafetyState& s,const SafetyMonitor&,float) { return s.allowed; }
bool otaPendingVerify() { return pending; }
bool lockI2cBus(int) { return true; }
void unlockI2cBus() {}
}
"""
suffix = r"""
int main() {
    using namespace mechadog;
    MotionHal m; MotionSafetyState s; SafetyMonitor safety;
    dispatch(m,s,safety,1); assert(m.step>0 && m.angle==0);
    dispatch(m,s,safety,2); assert(m.step<0 && m.angle==0);
    dispatch(m,s,safety,3); assert(m.step>0 && m.angle>0);
    dispatch(m,s,safety,4); assert(m.step>0 && m.angle<0);
    dispatch(m,s,safety,9); assert(m.stops==1);
    assert(!strcmp(voiceCmdName(2),"GO-BACKWARD"));
    assert(!strcmp(voiceCmdName(3),"TURN-LEFT"));
    assert(!strcmp(voiceCmdName(4),"TURN-RIGHT"));
    assert(!strcmp(voiceCmdName(29),"MARCH"));
    s.allowed=false; int before=m.moves;
    dispatch(m,s,safety,1); assert(m.moves==before);
    dispatch(m,s,safety,11); assert(m.action_id==-1);
    s.allowed=true; pending=true;
    dispatch(m,s,safety,2); assert(m.moves==before);
    dispatch(m,s,safety,10); assert(m.action_id==-1);
    pending=false; dispatch(m,s,safety,0xe0); assert(m.moves==before);
    puts("PASS: actual robot dispatcher directions, labels, stop, safety/OTA gates, unknown IDs");
}
"""
test = ROOT / "test_robot_mapping.cpp"
test.write_text(prefix + source + suffix, encoding="utf-8")
env = os.environ.copy()
env["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / "zig-local-cache")
env["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / "zig-global-cache")
exe = ROOT / "test_robot_mapping.exe"
with (ROOT / "robot-test-compile.log").open("wb") as out:
    subprocess.run(
        [
            str(ZIG),
            "c++",
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-O0",
            str(test),
            "-o",
            str(exe),
        ],
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=180,
    )
result = subprocess.run([str(exe)], capture_output=True, check=True, timeout=10)
(ROOT / "robot-test.log").write_bytes(result.stdout + result.stderr)
print(result.stdout.decode())
