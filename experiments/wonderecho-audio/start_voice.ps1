param(
    [string]$Port = 'COM9',
    [switch]$GuardCheck,
    [string]$Python = 'C:\dev\mechadog-voice\pipeline\Scripts\python.exe',
    [string]$PiperModel = 'C:\dev\mechadog-voice\models\piper\ko_KR-kss-medium.onnx',
    [string]$Model = 'C:\dev\mechadog-voice\models\gguf\EXAONE-3.5-7.8B-Instruct-Q4_K_M.gguf'
)
# MechaDog 음성 대화 시작 — llama.cpp CUDA DLL 경로 + 파이프라인 기동
$site = & $Python -c 'import site; print(site.getsitepackages()[0])'
$env:PATH = "$site\torch\lib;$site\nvidia\cublas\bin;$site\nvidia\cudnn\bin;$site\nvidia\cuda_nvrtc\bin;$env:PATH"
Set-Location $PSScriptRoot

if ($GuardCheck) {
    & $Python -X utf8 voice_pipeline.py --port $Port --guard-check --whisper medium --piper-model $PiperModel $args
} else {
    # 가상 MES (보조 기능) — :8095가 안 떠 있으면 함께 기동
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:8095/api/health' -TimeoutSec 1 | Out-Null
    } catch {
        Start-Process -WindowStyle Hidden $Python `
            -ArgumentList '-X','utf8','factory_mes.py','--serve','--port','8095' `
            -WorkingDirectory $PSScriptRoot
    }
    & $Python -X utf8 voice_pipeline.py --port $Port --model $Model --piper-model $PiperModel --whisper medium --web 8090 --robot-id mechadog-01 $args
}
