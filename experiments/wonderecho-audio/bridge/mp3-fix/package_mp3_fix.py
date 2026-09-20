"""Official CI1302 packaging, preserved factory resources; no device access."""
import hashlib
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT=Path('<WE_SDK_TREE_MP3>')
SDK=ROOT/'offline-speaker-1.12.16'
BASE=Path('<SDK_ORIG_TREE>')
REPO=Path('<REPO_WORKTREE>')
sys.path.insert(0,str(REPO/'experiments/wonderecho-audio'))
from build_speaker_image import entries  # noqa: E402
from inspect_factory import inspect  # noqa: E402


def sha(data): return hashlib.sha256(data).hexdigest()
factory=BASE/'recovery/2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin'
raw=factory.read_bytes()
assert sha(raw)=='c4328480d8f9cbe2e15dde41bb7d170d93c46c96c884742394322db87b5d2b2d'
elf=SDK/'projects/offline_asr_sample/project_file/build/offline-stream.elf'
out=ROOT/('package-3601-'+sha(elf.read_bytes())[:12])
out.mkdir(exist_ok=False)
parts = out / 'user_code'
parts.mkdir()
objcopy=BASE/'toolchain/gcc_fix_raissrc/bin/riscv-nuclei-elf-objcopy.exe'
tool=SDK/'tools/ci-tool-kit.exe'
subprocess.run([str(objcopy),'-O','binary',str(elf),str(parts/'[0]code.bin')],check=True,timeout=30)
shutil.copy2(SDK/'libs/libfbin.a',parts/'[1]code.bin')
merged=subprocess.run([str(tool),'merge','user-file','-i',str(parts)],capture_output=True,check=True,timeout=30)
(out/'merge.log').write_bytes(merged.stdout+merged.stderr)
code=parts/'user_code.bin'
assert entries(code.read_bytes()) == {0:(parts/'[0]code.bin').read_bytes(),1:(parts/'[1]code.bin').read_bytes()}
original=inspect(factory,code)
assert original['fits_physical_code_space']
table=raw[8192:8470]
nv_off,nv_size=struct.unpack_from('<II',table,268)
assert nv_off+nv_size==2097152
resources = out / 'factory-parts'
resources.mkdir()
(resources/'boot.bin').write_bytes(raw[:8192])
for name,p in original['partitions'].items():
    (resources/(name+'.bin')).write_bytes(raw[p['offset']:p['offset']+p['size']])
images = out / 'image'
images.mkdir()
args=[str(tool),'mf','-f','v2','--chip-name','CI1302','--rom-size','2097152','--nvdata-size',str(nv_size),
      '--factory-id','100','--brand-id','100','--board-name','DEMO_Board','--hardware-version','2.0.0',
      '--firmware-name','WonderEcho_Bridge_Bench','--firmware-version','2.1.36','--boot-file',str(resources/'boot.bin'),
      '--user-code',str(code),'--user-code-version','3601','--user-code-size',str(original['code_space_before_next_partition'])]
for name,option in (('asr','asr'),('dnn','nn'),('voice','voice'),('user','user-file')):
    p=original['partitions'][name]
    args += ['--'+(option if name=='user' else option+'-file'),str(resources/(name+'.bin')),
             '--'+option+'-size',str((p['size']+4095)//4096*4096),'--'+option+'-version',str(p['version'])]
args+=['--output-path',str(images)]
packed=subprocess.run(args,capture_output=True,check=True,timeout=60)
(out/'packer.log').write_bytes(packed.stdout+packed.stderr)
files = list(images.glob('*.bin'))
assert len(files) == 1
image = files[0]
image_raw = image.read_bytes()
verified=inspect(image,code)
assert image_raw[:8192]==raw[:8192]
assert image_raw[8460:8468]==raw[8460:8468]
for name in ('asr','dnn','voice','user'):
    assert verified['partitions'][name]==original['partitions'][name],name
assert verified['partitions']['code1']['sha256']==sha(code.read_bytes())
target=Path('<OUT_DIR>/36-mp3.bin')
if target.exists():
    assert sha(target.read_bytes()) == sha(image_raw), 'Refusing to overwrite an earlier candidate'
else:
    target.write_bytes(image_raw)
verified.update(build=3601,version='2.1.36',image_path=str(target),image_sha256=sha(image_raw),
                boot_resources_nv_layout_preserved=True,official_code_parts_verified=True,
                uart0='SDK diagnostics at 921600; WEC1/PC audio disabled',
                uart1='WonderEcho bridge at 115200',supported_command_ids=[1,2,3,4,9,10,11,12,26,27,28,29,30],
                supported_broadcast_ids=[1,2,3,4,5],gain_limit=40,device_installed=False,runtime_verified=False)
(out/'verification.json').write_text(json.dumps(verified,indent=2)+'\n')
(ROOT/'candidate.json').write_text(json.dumps(verified,indent=2)+'\n')
print(json.dumps({'path':str(target),'sha256':sha(image_raw),'code_bytes':code.stat().st_size,
                  'image_bytes':len(image_raw),'verification':str(out/'verification.json')},indent=2))
