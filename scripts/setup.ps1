<#
  MechDog Physical AI — 개발 환경 세팅 (Windows / PowerShell)

  사용법:
      cd <저장소 루트>
      powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

  하는 일:
      1. Python 3.12 이상 확인
      2. .venv 생성
      3. requirements-dev.txt 설치
      4. pytest 로 검증
      5. ONNX Runtime 실행 프로바이더 확인
      6. 각 단계 소요 시간 출력

  기준값(2026-09-03, Python 3.12.10 기존 설치 · 캐시 없음): 약 40초
  이보다 크게 오래 걸리면 원인을 기록해 주세요 — DR-17 재검토 근거가 됩니다.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($name, $block) {
    $t = Get-Date
    & $block
    $sec = ((Get-Date) - $t).TotalSeconds
    Write-Host ("  {0,-28} {1,6:N1} s" -f $name, $sec) -ForegroundColor DarkGray
    return $sec
}

Write-Host "`n== MechDog 개발 환경 세팅 ==`n" -ForegroundColor Cyan
$total = 0

# ── 1. Python 확인 ──────────────────────────────
$ver = (python --version 2>&1) -replace 'Python\s*', ''
if (-not $ver) {
    Write-Host "python 명령을 찾을 수 없습니다." -ForegroundColor Red
    Write-Host "  https://www.python.org/downloads/ 에서 3.12 이상을 설치하고" -ForegroundColor Yellow
    Write-Host "  설치 화면의 'Add python.exe to PATH' 를 반드시 체크하세요." -ForegroundColor Yellow
    Write-Host "  Microsoft Store 버전은 권장하지 않습니다 (경로 문제)." -ForegroundColor Yellow
    exit 1
}
$parts = $ver.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 12)) {
    Write-Host "Python $ver 은 지원하지 않습니다. 3.12 이상이 필요합니다." -ForegroundColor Red
    exit 1
}
Write-Host "  Python $ver" -ForegroundColor Green

# ── 2. venv ─────────────────────────────────────
if (Test-Path .venv) {
    Write-Host "  .venv 가 이미 있습니다 — 재사용합니다" -ForegroundColor DarkGray
} else {
    $total += Step "venv 생성" { python -m venv .venv }
}
$py = Join-Path $root '.venv\Scripts\python.exe'

# ── 3. 의존성 ───────────────────────────────────
$total += Step "pip 업그레이드" { & $py -m pip install --quiet --upgrade pip }
$total += Step "의존성 설치" { & $py -m pip install --quiet -r requirements-dev.txt }

# ── 4. 검증 ─────────────────────────────────────
$total += Step "pytest" { & $py -m pytest -q }

# ── 5. 실행 프로바이더 ──────────────────────────
$eps = & $py -c "import onnxruntime as ort; print(','.join(ort.get_available_providers()))"
Write-Host "`n  사용 가능한 실행 프로바이더: $eps" -ForegroundColor Green
if ($eps -notmatch 'CPUExecutionProvider') {
    Write-Host "  CPU EP 가 없습니다 — 설치가 정상적이지 않습니다." -ForegroundColor Red
}

# ── 결과 ────────────────────────────────────────
Write-Host ("`n== 완료 · 총 {0:N1} 초 ==" -f $total) -ForegroundColor Cyan
Write-Host "기준값 약 40초. 크게 초과했다면 원인을 팀에 공유해 주세요 (DR-17)." -ForegroundColor DarkGray
Write-Host "`n다음: .venv\Scripts\activate 로 활성화하거나, 위 python 경로를 IDE 인터프리터로 지정하세요.`n"
