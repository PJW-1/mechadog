$ErrorActionPreference='Stop'
$voiceWork='<USER_HOME>\Desktop\공부\피지컬ai\dev\wonderecho-bridge-work'
$voiceRepo='C:\dev\mechadog-voicebridge-20260919\experiments\wonderecho-audio\bridge'
foreach ($voiceFile in Get-ChildItem -LiteralPath $voiceWork -File) {
    if ($voiceFile.Extension -in @('.c','.h','.py','.md','.ps1')) {
        $voiceTarget=Join-Path $voiceRepo $voiceFile.Name
        if (-not (Test-Path -LiteralPath $voiceTarget) -or
            (Get-FileHash -LiteralPath $voiceTarget).Hash -ne (Get-FileHash -LiteralPath $voiceFile.FullName).Hash) {
            Copy-Item -LiteralPath $voiceFile.FullName -Destination $voiceTarget -Force
        }
    }
}
Copy-Item -LiteralPath (Join-Path $voiceWork 'README.md') -Destination 'C:\dev\voice\34-bridge-README.md' -Force
