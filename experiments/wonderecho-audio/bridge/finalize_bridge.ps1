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
$voiceNotes=@{
'HANDOFF.md'=@'

## 2026-09-19 Codex — v34 브리지 후보 준비 · 설치 대기 · 구현 잠금 해제
- 사용자 연결/진행 승인으로 별도 worktree fix/wonderecho-bridge-20260919(<REPO_WORKTREE>, dev079d9ba), SDK <WE_SDK_TREE>에서 구현. 원본 감사 대상15파일 SHA 동일, latest=27 보존. 기존 다른 AI 잠금/파일 무변경.
- UART1 AA55/type/id/FB 송수신, 큐8·100ms 프레임 만료/500ms 이벤트 만료·TX20ms 한도·18개 명시 ID/리소스 검증·nativeIIC0 off 구현. gain최대40, NV 직접 변경 없음. 미검증 IDLE-ASR 강제정리 타이머 off. 지원 command13개+방송5개이며 전체공장명령 호환 아님.
- v34는 UART0@921600 진단 전용/WEC1 PC오디오 없음. voice_prompt 중복 trace 링크 문제를 제외 설정으로 해결, 비활성 인코더 강제링크 해제. PC STT/TTS/LLM 모델 무변경. USB 오디오 병행은 다음 단계.
- PC C 프로토콜/100000 잡음·18매핑/두공장그룹·실제 로봇 dispatcher를 가짜 장치로 컴파일한 방향/안전/OTA 거부 시험 통과. CI1302 컴파일 및 code0+libfbin 공식 merge/공장부트·ASR·DNN·voice·user·NV배치 보존 통과. SDK기존경고 남음(새 브리지 C는 Werror).
- 설치파일 <OUT_DIR>/34-bridge.bin, SHA09683d96d96127552bfea6c2fc68a329505346a2b6157cb1283c44ef29fe3eee, 버전2.1.34/로그3401, code150600B/이미지1969737B. candidate.json과 package-3401-e312fa103021/verification.json 근거. 설치/실물 검증은 아직 안 됨.
- 장치: PnP COM5·COM8 확인. COM8 부팅 로그로 로봇 직접 식별, 4핀 분리 상태 IIC1=6A/77. COM5 WEC1 응답 없음(설치버전 미확인). DTR/RTS=false 수신과 COM5 QUERY만, 명시적 리셋/이동/재생/녹음/플래시 없음; 부팅 로그를 관측했으므로 포트 열기의 리셋 무발생을 보증하지 않음. 모든포트 종료. 원본7개 SHA보관: 로컬05_실물_측정결과/2026-09-19/01_음성브리지_USB식별.
- 다음: 사용자가 PACK_UPDATE_TOOL로34설치·도구닫기 → UART0에서 [WE-BRIDGE] build=3401 ready/init_result=0 확인 → 모듈 USB제거·전원OFF에서4핀연결 → 구동차단으로 reg64 예상ID·reg6E 실제발화 시험. 당시 로컬 벤더 dispatcher 후보는 저장소 펌웨어가 아니며 로봇 설치 안 함. ESP32 소비 코드·병행 USB오디오·4핀PCM·간헐무음 해결/WBS완료로 표시 금지.
- 작업일지06, dev/wonderecho-bridge-work/README.md 참조. 현재 실행/포트 작업 없음으로 위 Codex 구현잠금 해제; 후보는 보존. 소스는 미커밋, PR/게시 없음. 로컬 경로가 들어간 빌드 helper는 게시 전 인자화 필요.
'@
'REVIEW_LOG.md'=@'

## 2026-09-19 Codex — 공장 브리지 결함 수정 후보 v34 (실물 미해결)
- 원본 무변경 별도 SDK에서 UART1 공장프레임/명시매핑을 구현하고 잘못된 nativeIIC0를 비활성. 손상/분할/만료/잡음100000 및 실제 로봇방향 함수를 가짜장치로 실행하는 회귀 통과. 원본 감사15파일 SHA 동일 확인.
- 코드 수정/CI1302 빌드/공식 패키징까지만 완료. 34-bridge.bin SHA09683d96… 공장부트·4개리소스·NV배치 동일, libfbin포함 확인. 설치·내부UART신호·소리 미검증이므로 결함의 실물 해결 상태는 미해결 유지.
- v34 UART0진단/WEC1off 명시. 새 송수신 패스에 임의 E* 제어를 연결하지 않았고 미지원ID 거부. 온보드 방송13+5 한정. USB·LLM 오디오 병행은 별도 작업. 로봇매핑 패치는 미배포.
- 별도 후보에서 trace 이중정의(현재 공유소스 clean build 문제)를 구성 정리로 해결; 원본SDK의 기존 경고/재생정체 가능성은 남음. 상세 로컬일지06/dev/wonderecho-bridge-work/README.md.
'@
'VERIFIED.md'=@'

## 2026-09-19 Codex — v34 빌드/PC 시험 및 USB 식별 근거
- Windows PnP와 SERIALCOMM: COM5·COM8 CH340 정상. COM8@115200 MechDog/I2C scan 직접 읽음, IIC1=6A/77·IIC2없음. COM5 WEC1 query 응답없음(현재설치본 미확인). 수신 원본7개 SHA보관, 기기플래시/이동/재생/녹음 없음.
- v34 source native C 패킷 회귀·잡음100000/18매핑2공장그룹 통과. 로봇 실제 dispatcher에 fake hardware를 붙여 방향/정지/안전·OTA 거부 회귀 통과(실물구동 아님). CI1302GCC 링크 통과.
- <OUT_DIR>/34-bridge.bin SHA25609683d96d96127552bfea6c2fc68a329505346a2b6157cb1283c44ef29fe3eee, 전체1969737B/code150600B. 공식패커/분할code0+libfbin merge/공장 boot asr dnn voice user 및NV배치 보존검사. <WE_SDK_TREE>/candidate.json 및 package-3401-e312fa103021/verification.json 참조.
- 설치·물리발화·4핀왕복·USB오디오회귀는 미검증. UART0로그/WEC1비활성 구성. latest=27(4bf078c4…) 및 원본소스15SHA 동일 재확인.
'@
'VOICE_VERSIONS.md'=@'

## 2026-09-19 Codex — v34 / 2.1.34 / build3401 공장 UART1 브리지 후보
| 항목 | 결과 |
|---|---|
| 파일 | <OUT_DIR>/34-bridge.bin |
| SHA256 | 09683d96d96127552bfea6c2fc68a329505346a2b6157cb1283c44ef29fe3eee |
| 크기 | code150600B, 전체1969737B |
| 수정 | UART1/115200/AA55 프레임, 명시 ID 매핑13명령+5방송, RX/TX 분리·큐/만료/오류처리; nativeIIC0off; gain≤40; IDLE 기반 실험watchdog off |
| UART0 | 921600 진단로그 전용 — WEC1/USB PC오디오 비활성. 기존27을 대체하는 대화용 이미지 아님 |
| 검증 | 네이티브 C/잡음100000·공장두그룹18매핑·가짜로봇 실제dispatcher 회귀·GCC링크·공식merge·부트/리소스/배치/NV 보존 통과 |
| 실물 | 미설치·미검증. [WE-BRIDGE] build=3401 ready / init_result=0 확인부터 필요 |
| 보존 | 원본SDK15파일SHA동일/latest=27유지/PC모델무변경 |
- 별도SDK <WE_SDK_TREE>, 소스 worktree <REPO_WORKTREE>. 원본 공유소스의 trace 중복정의는 이 후보에서 사용하지 않는 voice_prompt 객체 제외로 해결. 로봇방향패치는 후보만/미설치. 작업일지06 및 34-bridge-README.md 참조. 번호만 보고 기능 전체가 승계됐다고 추정하지 말 것.
'@
}
foreach ($voiceName in $voiceNotes.Keys) {
    $voiceLog=Join-Path 'C:\dev\_ai_collab' $voiceName
    $voiceText=[string]$voiceNotes[$voiceName]
    $voiceHeading=($voiceText.TrimStart() -split "`n")[0]
    if (-not [IO.File]::ReadAllText($voiceLog).Contains($voiceHeading)) {
        [IO.File]::AppendAllText($voiceLog,"`n"+$voiceText+"`n",[Text.UTF8Encoding]::new($false))
    }
    Write-Output "Recorded $voiceName"
}
