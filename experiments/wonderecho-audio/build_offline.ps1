param(
    [string]$DevelopmentRoot = 'C:/dev/mechadog-voice-20260913',
    [switch]$WithEncoder,
    [switch]$WithStream
)
$ErrorActionPreference = 'Stop'
$sdkName = if ($WithStream) { 'offline-stream-1.12.16' } elseif ($WithEncoder) { 'offline-codec-1.12.16' } else { 'offline-ci1302-1.12.16' }
$artifact = if ($WithStream) { 'offline-stream' } elseif ($WithEncoder) { 'offline-codec' } else { 'offline-ci1302' }
$sdk = Join-Path $DevelopmentRoot $sdkName
$project = Join-Path $sdk 'projects/offline_asr_sample/project_file'
$bin = Join-Path $DevelopmentRoot 'toolchain/gcc_fix_raissrc/bin'
$log = Join-Path $DevelopmentRoot ($artifact + '-build.log')
if (-not (Test-Path -LiteralPath (Join-Path $sdk 'candidate-manifest.json'))) {
    throw 'Run prepare_offline.py first. This builds only an ELF and never flashes.'
}
$previousPath = $env:PATH
try {
    $env:PATH = $bin + ';' + (Join-Path $sdk 'tools/build-tools/bin') + ';' + $env:PATH
    if ($WithEncoder -or $WithStream) {
        if (-not (Test-Path -LiteralPath (Join-Path $sdk 'codec-manifest.json'))) {
            throw 'Prepare the encoder candidate first'
        }
        $codec = Join-Path $sdk 'components/cias_speex'
        & (Join-Path $bin 'riscv-nuclei-elf-gcc.exe') -std=c11 -march=rv32imafc -mabi=ilp32f -Wall -Wextra -Werror -c `
            (Join-Path $sdk 'projects/offline_asr_sample/src/voice_encoder.c') `
            ('-I' + $codec + '/include') ('-I' + $codec + '/include/speex') `
            ('-I' + $codec + '/port') ('-I' + $codec + '/libspeex') `
            -o (Join-Path $sdk 'voice_encoder-strict.o')
        if ($LASTEXITCODE -ne 0) { throw 'Strict encoder adapter compilation failed' }
    }
    if ($WithStream -and -not (Test-Path -LiteralPath (Join-Path $sdk 'stream-manifest.json'))) {
        throw 'Prepare the stream candidate first'
    }
    Push-Location -LiteralPath $project
    try {
        $makeArgs = @('-j4')
        if ($WithStream) {
            Set-Content -LiteralPath 'strict_stream.mk' -Encoding ascii -Value 'build/objs/voice_stream.o: C_FLAGS += -Wall -Wextra -Werror'
            $makeArgs += @('-f', 'makefile', '-f', 'strict_stream.mk')
        }
        $makeArgs += @(('PROJECT_NAME=' + $artifact), ('build/' + $artifact + '.elf'))
        & (Join-Path $sdk 'tools/build-tools/bin/make.exe') @makeArgs *> $log
        if ($LASTEXITCODE -ne 0) { throw "Build failed; see $log" }
        & (Join-Path $bin 'riscv-nuclei-elf-size.exe') ('build/' + $artifact + '.elf')
        if ($LASTEXITCODE -ne 0) { throw 'ELF inspection failed' }
        Write-Output "Compile-only ELF built. NOT FLASH READY. Log: $log"
    } finally { Pop-Location }
} finally { $env:PATH = $previousPath }
