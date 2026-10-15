# 위험물 로봇 시점 합성 학습 (2026-10-06, 로컬 전용)
- 원본: Roboflow Universe vision-wajsm/lighter-wvso3 100장, vision-wajsm/portable-charger 91장 (CC BY 4.0, hazard-v1 과 같은 출처) → GrabCut 오려내기(make_cutouts.py), 불량 38장 제외
- 실물 크롭: 10-06 리허설3 로봇 프레임 3개(보조배터리 눕힘·세움, 라이터 세움) ×15 가중
- 배경: 10-06 로봇 프레임 3,645장(위험물 시험 구간 제외), 물체 붙인 3,000 + 없음 1,200 (make_synth.py, seed 20261006)
- 학습: <YOLOX 저장소> + <yolox venv>, YOLOX-S, COCO yolox_s.pth 시작, 30 epoch, b16 fp16 (exp_hazard_synth.py) → best AP50:95 0.769(합성 val, 실물 성능 아님)
- 산출: out/hazard.onnx (= hazard_synth_v2_20261006.onnx), sha256 06e6274aaf8f04a5…, 출력 [1,8400,7]
- 실물 9장 비교(compare.py, compare.jpg): 의자·PC·쓰레기통 오검출 0, 라이터 미학습 사진 0.87·0.88, 보조배터리 0/5, 작은 오검출 1건(0.54)
