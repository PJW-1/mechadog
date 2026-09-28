---
name: ppe-pc-test
description: 실제 XIAO 카메라로 PPE 검수를 실행한다. XIAO를 쓸 수 없을 때만 PC 웹캠 사전 관찰로 전환한다.
---

# PPE 검수

정본 절차는 `docs/PPE_ACCEPTANCE.md`, 시나리오는 `config/ppe_acceptance.json`이다.
이 스킬에는 프로젝트 규칙을 복제하지 않고 실행 방법만 둔다.

## XIAO 실기 시작

사용자에게 시험 개체와 XIAO IP를 확인하고 다음처럼 실행한다. `--device`를 생략하지 않는다.

```bash
python tools/ppe_live_check.py --device <unit-id> --xiao-ip <ip> \
  --seconds 900 --segments --scenario xiao --web-port 8008 \
  --report TEST_MECHDOG/results/<YYYYMMDD>_ppe-xiao/summary.md \
  --session TEST_MECHDOG/results/<YYYYMMDD>_ppe-xiao/session.json
```

설정 정본인 `violation_window_ms`와 `violation_hits_required`를 덮어쓰지 않는다. 브라우저에서
현재 조건에 맞는 구간을 누르고, 화면 안내에 따라 방향을 바꾼다. 이 도구는 읽기 전용이며
로봇 자세나 이동 명령을 보내지 않는다. `pitch_up`·`sit`·`back_off` 구간은 별도의 안전한
조작 경로로 해당 자세가 된 것을 확인한 뒤 선택한다.

## 정상 종료

브라우저의 **시험 종료 및 결과 저장** 버튼을 누른다. 브라우저를 쓸 수 없으면 다음 요청을
보낸다.

```powershell
Invoke-WebRequest -Method Post http://127.0.0.1:8008/stop
```

`Stop-Process -Force`를 쓰지 않는다. 강제 종료는 MD와 최종 JSON을 남길 기회를 없앤다.

## PC 웹캠 대체 관찰

XIAO를 사용할 수 없을 때만 `--webcam 0 --scenario webcam`으로 실행할 수 있다. 느린 PC에서
시간 창을 덮어쓸 수 있지만 그 결과는 FR-9 합격 근거가 아니다. 기준 PC의 8.2ms 기록은
COCO 1단 검출만의 값이며, PPE를 포함한 2단 전체 성능은 XIAO 실기에서 별도로 측정한다.
