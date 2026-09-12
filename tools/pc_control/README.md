# Windows 로봇 유지보수 도구

개인 PC에서 사용한 Wi-Fi 업데이트 화면과 USB 모드 전환 도구를 설정 파일로 분리한 게시 후보다. AI 추론이나 보행 기능을 추가하지 않는다. 현재 **검토된 센서 ON / 구동 OFF OTA 기체** 전용이며 일반 ESP32 업로더가 아니다.

## 설정과 실행

1. 저장소 밖에 개인 설정 폴더를 만들고 `settings.example.json`을 복사한다.
2. 실제 확인한 기체의 MAC·COM 포트, 인증된 OTA client JSON, 검토 패키지와 파티션 파일 경로를 입력한다. 상대 경로는 설정 파일 기준이다. 예제의 자리표시자는 실행되지 않는다.
3. 기존 로컬 도구를 함께 쓴다면 `lock_file`을 **기존 도구의 operation.lock과 같은 파일**로 지정한다. 여러 설정도 같은 기체에는 같은 잠금을 사용한다.
4. Windows에서 저장소의 Python 가상환경을 준비한다. USB 도구는 `python -m pip install -r tools/pc_control/requirements.txt`로 의존성을 설치한다. Wi-Fi 도구는 표준 라이브러리와 Tk가 필요하다.

```powershell
$env:MECHADOG_TOOL_CONFIG = 'D:/robot-private/settings.json'
python tools/pc_control/robot_ota.pyw
# 별도 USB 모드 도구 — 연결된 COM 포트가 음성 모듈이 아닌 본체인지 먼저 확인
python tools/pc_control/robot_modes.pyw
```

Wi-Fi 화면: 연결 확인 → 검토된 package.json 선택 → 업데이트. 인증서 pin·기체 MAC·이미지 SHA 확인과 재부팅 후 확정은 기존 `tools/ota_update.py`가 담당한다. 자동 탐색이나 자동 업데이트는 하지 않는다. GUI 작업은 별도 프로세스에서 실행한다.

USB 화면은 상태 확인과 명시적인 다운로드/일반부팅 전환만 지원한다. flash를 쓰지 않는다. 기체·앱·파티션 검증에 실패하면 리셋을 거부한다. 자동 BOOT 진입이 모든 보드에서 가능하다는 뜻이 아니다.

USB 검증은 `firmware_mechdog_motion/OTA.md`의 보존형 OTA 배치만 허용한다. 순정 배치나 다른 개체의 레이아웃에는 사용하지 않는다. 최초 OTA 배치 전환·키 발급·복구 이미지 생성은 이 도구의 기능이 아니다.

## 검증 범위

원래 PC 설치본의 모드/이미지 가드 모의시험 20개가 통과했다. 설정을 분리한 게시본은 별도 오프라인 시험 대상으로 검증한다. 실제 로봇에 이번 게시본을 새로 실행하거나 USB reset/flash하지 않았다. 이전 로컬 Wi-Fi 클라이언트의 실제 설치 결과는 `docs/measurements/2026-09-13-stationary-ota.md`에 있다. 원래 설치본의 실측을 설정 분리본의 전체 실물 검증으로 간주하지 않는다.

설정·토큰·키·BIN/ELF·실측 원본·runtime 로그는 저장소 밖에 보관한다. 사용자 PC에 이미 설치된 도구는 이 PR로 교체하지 않는다.
