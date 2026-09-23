param(
    [string]$Port = 'COM9',
    [switch]$GuardCheck,
    [string]$Python = 'C:\dev\mechadog-voice\pipeline\Scripts\python.exe',
    [string]$PiperModel = 'C:\dev\mechadog-voice\models\piper\ko_KR-kss-medium.onnx'
)
# MechaDog 음성 대화 시작 — GPU DLL 경로는 파이프라인이 등록한다.
Set-Location $PSScriptRoot

if ($GuardCheck) {
    & $Python -X utf8 voice_pipeline.py --port $Port --guard-check --whisper medium --piper-model $PiperModel $args
} else {
    & $Python -X utf8 voice_pipeline.py --port $Port --piper-model $PiperModel --whisper medium --web 8090 --robot-id mechadog-01 $args
}
