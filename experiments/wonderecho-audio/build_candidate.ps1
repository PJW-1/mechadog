param([Parameter(Mandatory = $true)][string]$DevelopmentRoot)
$ErrorActionPreference = 'Stop'
$sdk = Join-Path $DevelopmentRoot 'candidate-sdk-2.2.7'
$project = Join-Path $sdk 'projects/offline_asr_llm_aiot_uart_sample/project_file'
$bin = Join-Path $DevelopmentRoot 'toolchain/gcc_fix_raissrc/bin'
$make = Join-Path $sdk 'tools/build-tools/bin/make.exe'
$log = Join-Path $DevelopmentRoot 'build-ci1302-candidate.log'
if (-not (Test-Path -LiteralPath (Join-Path $sdk 'candidate-manifest.json'))) {
    throw 'Run prepare_ci1302.py first. This script builds an ELF only, never flashes.'
}
$previousPath = $env:PATH
try {
    $env:PATH = $bin + ';' + (Join-Path $sdk 'tools/build-tools/bin') + ';' + $env:PATH
    Push-Location -LiteralPath $project
    try {
        & $make -j4 'PROJECT_NAME=ci1302-candidate' 'build/ci1302-candidate.elf' *> $log
        if ($LASTEXITCODE -ne 0) { throw "Build failed; see $log" }
        & (Join-Path $bin 'riscv-nuclei-elf-size.exe') 'build/ci1302-candidate.elf'
        if ($LASTEXITCODE -ne 0) { throw 'ELF size inspection failed' }
        Write-Output "Compile-only candidate built. NOT FLASH READY. Log: $log"
    } finally { Pop-Location }
} finally { $env:PATH = $previousPath }
