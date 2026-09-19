# PPE XIAO 검수 절차

이 문서는 `tools/ppe_live_check.py`로 실제 기체의 XIAO 영상을 관찰하는 절차다. 도구는
읽기 전용이며 로봇에 자세·이동·경고 명령을 보내지 않는다. 시험 구간의 정본은
`config/ppe_acceptance.json`이다.

## 준비

- 시험 개체의 `--device`와 XIAO IP를 확인한다.
- `models/coco.onnx`와 `models/ppe.onnx`를 준비한다.
- PPE 모델은 3,654,680바이트, SHA-256
  `ee46da018e8d35c60b41f89dfca33e47786d4e487bb942d78405557a7457db13`인지 확인한다.
- XIAO가 실제 기체의 15cm 장착 위치에 있고 다른 스트림 클라이언트가 붙어 있지 않은지
  확인한다.

## 실행

```powershell
python tools/ppe_live_check.py `
  --device mechdog-01 `
  --xiao-ip 192.168.0.42 `
  --seconds 900 `
  --segments `
  --scenario xiao `
  --web-port 8008 `
  --report TEST_MECHDOG/results/20260920_ppe-xiao/summary.md `
  --session TEST_MECHDOG/results/20260920_ppe-xiao/session.json
```

실제 개체명·IP·날짜로 바꿔 실행한다. XIAO 검수에서는 `--window-ms`와 `--hits`를 주지
않는다. `config/config.yaml`의 운영값으로 알람 창을 확인해야 한다.

브라우저에서 구간을 선택한 뒤 네 방향을 차례로 관찰한다. 직립과 웅크림은 전신이 화면에
들어온 상태에서 각각 네 가지 착용 조건을 수행한다. `clipped-base`는 머리가 잘렸을 때
`확인불가`가 나오는지 확인한다. `pitch-up`·`sit`·`back-off`는 별도의 안전한 조작 경로로
해당 단계에 도달한 다음 선택하며, 필요한 단계에서 전신 판정이 회복되면 이후 단계는
불필요하게 실행하지 않는다.

종료할 때 브라우저의 **시험 종료 및 결과 저장**을 누른다. 정상 종료되면 사람이 읽는
`summary.md`와 재계산 가능한 `session.json`이 함께 남는다.

## 결과 읽기

- **판정 가능률**: 확인불가가 아닌 판정 / 전체 판정
- **조건부 정확도**: 확인불가를 제외하고 기대 상태와 일치한 비율
- **실효 성공률**: 기대 상태와 일치한 판정 / 전체 판정

조건부 정확도만으로 통과를 주장하지 않는다. 카메라가 낮을 때 확인불가가 많으면 정확도가
높아도 실제 기능은 판정을 거의 내리지 못한다. 최종 결과에는 모델 해시, 개체 프로파일,
운영 알람 창, 구간별 원자료가 모두 있어야 한다.

## 현재 상태와 다음 작업

현재 상태는 **XIAO PPE 검수를 제대로 수행할 준비 완료**다. 아직 PPE 기능 전체 완료는
아니며, 다음 세션에서는 아래 작업을 이어서 수행한다.

1. **실제 XIAO 실기 수행**: 실제 기체에서 운영 설정 `1500ms / 3회`로 위 절차를 수행하고
   `summary.md`와 `session.json`을 수집한다.
2. **`host/vision/ppe_detector.py` 구현**: 실제 운용 판정기를 추가해 현재 닫혀 있는 factory
   모드의 PPE 의존성을 충족한다.
3. **런타임 통합**: 주 대상의 추적 ID별 판정 귀속과 중복 방지, 자세 상승 호출,
   `PPE_VIOLATION`·`PPE_SETTLED`·`PPE_UNDETERMINED` 사건 발행을 구현한다.
4. **종단 검증**: 공장 모드에서 사람 발견 → 정지 확인 → 클리핑 검사 → 자세 상승 →
   PPE 판정 → L3 또는 순찰 복귀까지 실제 흐름을 검증한다.
5. **합격 기준 확정**: XIAO 실측 결과를 바탕으로 판정 가능률의 합격 하한과 오알람률의
   합격 상한을 명시한다.

위 다섯 항목과 관련 회귀 시험을 모두 통과한 뒤에만 WBS 3.7.3과 PPE 기능 전체 완료를
선언한다.
