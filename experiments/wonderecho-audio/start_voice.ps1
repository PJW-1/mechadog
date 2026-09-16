# MechaDog 음성 대화 시작 — llama.cpp CUDA DLL 경로 + 파이프라인 기동
$site = 'C:\Users\a9800\AppData\Local\Programs\Python\Python312\Lib\site-packages'
$env:PATH = "$site\torch\lib;$site\nvidia\cublas\bin;$site\nvidia\cuda_nvrtc\bin;$env:PATH"
Set-Location $PSScriptRoot

# 가상 MES (보조 기능) — :8095가 안 떠 있으면 함께 기동
try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:8095/api/health' -TimeoutSec 1 | Out-Null
} catch {
    Start-Process -WindowStyle Hidden python `
        -ArgumentList '-X','utf8','factory_mes.py','--serve','--port','8095' `
        -WorkingDirectory $PSScriptRoot
}

python -X utf8 voice_pipeline.py --port COM5 --model 'C:\dev\voice\models\EXAONE-3.5-7.8B-Instruct-Q4_K_M.gguf' --whisper medium --web 8090 --robot-id mechadog-01 $args
