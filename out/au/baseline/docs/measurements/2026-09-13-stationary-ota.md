# 정지 OTA 실측 요약 — 2026-09-13

WBS 2.1.3·6.3.1의 부분 근거. 사용자가 게시를 요청한 측정 요약이며 개인 일지/원본 로그를 복사한 문서가 아니다.

## 조건과 직접 관측

- Host Windows PC와 ESP32가 같은 Wi-Fi LAN. Git dev4870adf의 센서ON/구동OFF/OTAON, Arduino ESP32 2.0.12. 앱923040B, 슬롯983040B.
- 이전 확정 앱에서 `sync-20260913`으로 TLS pin·기체 확인 후 OTA. 재부팅/건강 판정/인증된 PC 승인 후 새 이미지 확정, 마지막 상태 조회까지 동일 부팅 유지. 모터·USB 조작 없음.
- 35초 정지 통신: 텔레메트리350건, 순번1~350, 누락0, 10.007Hz, 최대 수신간격156ms, 단일 부팅.
- STOP259건 / 정상 ACK258건. 순번225 응답1건 미수신. 명령 자체 미도착인지 ACK 경로 손실인지 이번 근거로 구분할 수 없다. 최대 관측 ACK 왕복157ms.
- 명령 중단 후 link_ok=false20건, FAILSAFE 및 safety_latched=true 유지. PC 수신 소켓 종료 후 동일 포트 재바인딩 확인.
- 명령시각을 기준으로 붙인 텔레메트리 타임스탬프 나이 최대155ms. 이것은 센서 획득 시각 또는 카메라/AI 전체 지연 측정이 아니다.
- 배터리 표시7.536~7.568V, 별도 계측기 교정 시험 아님.

## 판정과 남은 검증

OTA 설치는 성공. 텔레메트리 주기·연속성은 통과했지만 ACK1건 미수신 때문에 전체 통신 검증은 **미통과**다. 실패 원본을 보존했다. 장시간/복수기체/접속장애·재접속/실보행/카메라 E2E250ms는 이 시험의 완료 범위가 아니다. 공식 WBS 완료 상태를 바꾸지 않는다.

재사용 후보 `tools/pc_control/verify_stationary.py`는 검토 패키지와 현재 기체의 일치·건강·확정 확인 후 STOP만30초 보내고5초간 명령 중단 상태를 관측한다. MOVE/RESET_SAFE/USB/firmware write는 하지 않는다. 별도 private 출력 폴더에 원본과 성공·실패 요약을 모두 기록한다. 기존 telemetry_probe/ota_update를 재사용하며 게시용 경로 변경본은 이번에 기기에서 재실행하지 않았다.

```powershell
$env:MECHADOG_TOOL_CONFIG = 'D:/robot-private/settings.json'
python tools/pc_control/verify_stationary.py --output D:/robot-private/measurements/new-run
```

고정35초 시험이며 로봇 IP를 다른 개체로 임의 변경하지 않는다. 수신 포트5101은 다른 수신기와 동시에 사용할 수 없다. 같은 기체 도구끼리는 settings의 lock_file을 공유한다.
