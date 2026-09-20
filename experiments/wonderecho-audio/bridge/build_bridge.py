"""Compile and test the isolated candidate. Does not flash or open COM ports."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path('<WE_SDK_TREE>')
SOURCE=Path(__file__).resolve().parent
SDK=ROOT/'offline-speaker-1.12.16'
PROJECT=SDK/'projects/offline_asr_sample/project_file'
TOOLCHAIN=Path('<GCC_TOOLCHAIN_BIN>')
ZIG=Path('<ZIG_EXE>')

def run(args, log, cwd=None, env=None):
    with (ROOT/log).open('wb') as f:
        p=subprocess.run([str(x) for x in args],cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,timeout=240)
    if p.returncode:
        print((ROOT/log).read_text(encoding='utf-8',errors='replace')[-8000:])
        raise SystemExit(p.returncode)

run([sys.executable,SOURCE/'prepare_bridge.py'],'prepare.log')
env=os.environ.copy()
env['ZIG_LOCAL_CACHE_DIR']=str(ROOT/'zig-local-cache')
env['ZIG_GLOBAL_CACHE_DIR']=str(ROOT/'zig-global-cache')
run([ZIG,'cc','-std=c11','-Wall','-Wextra','-Werror','-O1','-g',
     SOURCE/'we_bridge_protocol.c',SOURCE/'test_bridge_protocol.c','-o',ROOT/'test_bridge_protocol.exe'],
    'native-compile.log',env=env)
run([ROOT/'test_bridge_protocol.exe'],'native-test.log')
print((ROOT/'native-test.log').read_text())
env['PATH']=str(TOOLCHAIN)+';'+str(SDK/'tools/build-tools/bin')+';'+env['PATH']
(PROJECT/'build').mkdir(exist_ok=True)
run([SDK/'tools/build-tools/bin/make.exe','PROJECT_NAME=offline-stream','-f','makefile','-f','strict_stream.mk',
     '-j6','build/offline-stream.elf'],'firmware-build.log',cwd=PROJECT,env=env)
run([TOOLCHAIN/'riscv-nuclei-elf-size.exe',PROJECT/'build/offline-stream.elf'],'firmware-size.log')
print((ROOT/'firmware-size.log').read_text())
(ROOT/'build-result.json').write_text(json.dumps({'build':3401,'native_protocol_tests':'passed',
    'firmware_compile':'passed','device_installed':False,'runtime_verified':False,
    'uart0':'SDK diagnostic log (WEC1 disabled)','uart1':'WonderEcho 115200 bridge'},indent=2)+'\n')
