# MechaDog 음성 대화 시작 — llama.cpp CUDA DLL 경로 + 파이프라인 기동
$site = 'C:\Users\a9800\AppData\Local\Programs\Python\Python312\Lib\site-packages'
$env:PATH = "$site\torch\lib;$site\nvidia\cublas\bin;$site\nvidia\cuda_nvrtc\bin;$env:PATH"
Set-Location $PSScriptRoot
python -X utf8 voice_pipeline.py --port COM5 --model 'C:\dev\voice\models\EXAONE-3.5-7.8B-Instruct-Q4_K_M.gguf' --whisper medium --web 8090 --robot-id mechadog-01 $args
