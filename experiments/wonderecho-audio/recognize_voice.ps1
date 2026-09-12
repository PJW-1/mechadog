param(
    [Parameter(Mandatory = $true)][string]$Port,
    [Parameter(Mandatory = $true)][string]$DevelopmentRoot,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [switch]$Prompt
)
$ErrorActionPreference = 'Stop'
$voicePython = Join-Path $DevelopmentRoot 'pc-tools/Scripts/python.exe'
$voiceModel = Join-Path $DevelopmentRoot 'models/faster-whisper-medium'
if (-not (Test-Path -LiteralPath $voicePython) -or
    -not (Test-Path -LiteralPath (Join-Path $voiceModel 'model.bin'))) {
    throw 'Install the PC environment and approved local medium model first.'
}
$voiceNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'Korea Standard Time')
$voiceDay = Join-Path $OutputRoot $voiceNow.ToString('yyyy-MM-dd')
$voiceOutput = Join-Path $voiceDay ('WonderEcho_identity_' + $voiceNow.ToString('HHmmssfff'))
$captureArgs = @('--port', $Port, '--out', $voiceOutput)
if ($Prompt) {
    $captureArgs += '--prompt'
    Write-Host '모듈의 안내 음성이 끝난 뒤 신원을 말씀해 주세요. 이어서 5초 녹음합니다.'
} else { Write-Host '확인한 음성 모듈의 COM 포트만 사용하세요. 지금부터 5초 동안 신원을 말씀해 주세요.' }
& $voicePython -X utf8 (Join-Path $PSScriptRoot 'stream_client.py') @captureArgs
if ($LASTEXITCODE -ne 0) { throw "Capture failed. Original log: $voiceOutput" }
Write-Host '녹음 완료. PC에서 한국어를 인식합니다.'
& $voicePython -X utf8 (Join-Path $PSScriptRoot 'transcribe_local.py') --model $voiceModel $voiceOutput
if ($LASTEXITCODE -ne 0) { throw "Local recognition failed. Original audio: $voiceOutput" }
Write-Host "결과: $voiceOutput"
