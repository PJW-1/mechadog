# 4.7.7 경비모드 음성 신원 확인 실기 — 2026-09-22

- 장치: WonderEcho USB 시리얼 COM9, Host PC RTX 3080.
- 경로: `voice_pipeline.py --guard-check` → Piper 질문 재생 → WonderEcho 마이크 → faster-whisper medium → 데모 명단 규칙 대조 → Piper 응답 재생. 대화 LLM 미사용.
- 등록 이름: 발화 `김민수입니다` → STT `김민수입니다.` → `김민수 님, 신원이 확인되었습니다. 안전한 작업 되십시오.`
- 미등록 이름: 발화 `홍길동입니다` → STT `홍길동 입니다.` → `미등록 인원으로 확인됩니다. 출입 관리 절차에 따라 안내 데스크로 이동해 주세요.`
- 사용자 청취 확인: 질문과 확인·거절 답변이 모두 실제 스피커에서 들림.
- 처음 실행은 CUDA DLL 경로 누락으로 STT에서 실패. `nvidia/cublas/bin` 및 `nvidia/cudnn/bin`을 PATH에 넣은 뒤 두 회차 모두 정상 종료. `start_voice.ps1`에 경로를 반영함.

범위: `knowledge/직원명단.txt`는 합성 데모 명단이다. 말한 이름을 등록 명단과 대조한 안내 시나리오일 뿐 실제 신원 인증이나 로봇 `AUTH_OK`가 아니다. 로봇 움직임·사원증 인증은 이 실기에서 시험하지 않았다.
