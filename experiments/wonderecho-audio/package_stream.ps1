param(
    [string]$DevelopmentRoot = 'C:/dev/mechadog-voice-20260913',
    [string]$OutputName = 'offline-stream-package'
)
$ErrorActionPreference = 'Stop'
$sdk = Join-Path $DevelopmentRoot 'offline-stream-1.12.16'
if ($OutputName -notmatch '^offline-stream-package(-[A-Za-z0-9]+)*$') { throw 'Invalid package output name' }
$output = Join-Path $DevelopmentRoot $OutputName
if (Test-Path -LiteralPath $output) { throw 'Package destination exists; refusing overwrite' }
$elf = Join-Path $sdk 'projects/offline_asr_sample/project_file/build/offline-stream.elf'
if (-not (Test-Path -LiteralPath $elf)) { throw 'Build the stream ELF first' }
New-Item -ItemType Directory -Path $output | Out-Null
$parts = Join-Path $output 'user_code'
New-Item -ItemType Directory -Path $parts | Out-Null
& (Join-Path $DevelopmentRoot 'toolchain/gcc_fix_raissrc/bin/riscv-nuclei-elf-objcopy.exe') -O binary $elf (Join-Path $parts '[0]code.bin')
if ($LASTEXITCODE -ne 0) { throw 'ELF conversion failed' }
Copy-Item -LiteralPath (Join-Path $sdk 'libs/libfbin.a') -Destination (Join-Path $parts '[1]code.bin')
# Manufacturer's documented user-code merge; no flash or full model package.
& (Join-Path $sdk 'tools/ci-tool-kit.exe') merge user-file -i $parts
if ($LASTEXITCODE -ne 0) { throw 'Official user-code merge failed' }
Get-ChildItem -LiteralPath $parts -File | Get-FileHash -Algorithm SHA256 |
    Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'hashes.json') -Encoding utf8
Set-Content -LiteralPath (Join-Path $output 'NOT_FLASH_READY.txt') -Encoding utf8 -Value @'
Compile candidate only. Not a full factory image. Do not flash yet.
Requires verified WonderEcho board/USB UART mapping and compatible original
resource partition sizes/model ABI. Firmware installation and real audio
capture have NOT been validated. Original model files were not repackaged.
'@
Get-ChildItem -LiteralPath $parts -File | Select-Object Name,Length
