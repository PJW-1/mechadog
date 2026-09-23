# PPE v12: 5종 공동학습 개발 후보

[모델·평가·출처 릴리스](https://github.com/PJW-1/mechadog/releases/tag/ppe-v12-candidate-20260924)

안전모/미착용, 조끼/미착용, person_down을 함께 학습한 YOLOX-S 후보다.
기본 운영 모델은 ppe-v3로 유지한다. 실기 승인이나 WBS 실측 완료를 뜻하지 않는다.

## 받기

```bash
python tools/fetch_models.py --ppe-candidate
python tools/fetch_models.py --ppe-candidate --check
```

모델과 NOTICE를 `models/candidates/ppe-v12/`에 저장하고 크기와 SHA256을 검증한다.
`models/ppe.onnx`와 운영 설정은 변경하지 않는다. 비교 시험에서만 별도 모델 경로를 지정한다.
입력은 기존 YOLOX 전처리를 사용하며 float32 `[1,3,640,640]`, 출력은 raw `[1,8400,10]`이다.
클래스 순서는 `helmet, no_helmet, vest, no_vest, person_down`이다.

모델은 35,781,202바이트이며 SHA256은
`181ee940a5b8fd568fb6a231344eaf26484c0bf167cc869350eb8ccaf98caad4`다.
PyTorch/ONNX 출력·검출 대조 3장 통과 기록과 모델 카드는 릴리스에 있다.

## 선정 근거와 한계

동일 실사 369장, confidence 0.5 / IoU 0.5 기준 재현율이다.

| 항목 | v12 | v16 (8회) | v20 (8회) |
| --- | ---: | ---: | ---: |
| 안전모 | 60.97% | 60.71% | 63.27% |
| 안전모 미착용 | 67.86% | 64.29% | 65.82% |
| 조끼 | 82.51% | 83.41% | 81.61% |
| 조끼 미착용 | 46.63% | 51.12% | 46.07% |
| 정상 인물 크롭의 쓰러짐 오경고 | 2/80 | 3/80 | 4/80 |

v12는 정상 자세 오경고와 PPE 유지의 절충안이며 모든 지표에서 최고는 아니다.
PPE 전용 v8은 안전모 72.45%, 조끼 84.30%로 더 높지만 person_down이 없는 4종 모델이다.

v12의 합성 쓰러짐 정답 검출은 전체화면/정답 인물 크롭 모두 18/18이다.
그러나 최근 UR 실사 쓰러짐 진단에서는 **4프레임 모두 미검출**했다(v16 1/4, v20 2/4).
선별된 상관 프레임으로 독립 사건 정확도가 아니며, 잘림·가림 조건이 포함된다.
합성 결과로 실제 쓰러짐 검증을 대신할 수 없다. XIAO/로봇 현장 시험은 미완료다.

## 출처

YOLOX-S(Megvii, Apache-2.0)를 기반으로 실사 PPE2286 v6
([Mendeley DOI10.17632/zkzghjvpn2.6](https://data.mendeley.com/datasets/zkzghjvpn2/6),
Mei-Ling Huang·Ying Cheng, CC BY4.0), Simuletic 합성 쓰러짐,
AI Hub 71641 정상 자세를 사용했다. v12 쓰러짐 양성 학습은 합성 자료다.
UR 실사는 진단에만 사용했다. 원본 데이터는 배포하지 않는다.
가중치의 무제한 상업 이용을 보장하지 않으며 릴리스 NOTICE와 각 데이터 이용 조건을 확인한다.
