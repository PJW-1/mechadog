# WonderEcho v36 — 공장 MP3 음원 재생 지원 복원

**코드 결함 재현·수정·빌드 완료. 기기 미설치, 실제 소리 확인은 남아 있다.**

- 설치 파일: `<OUT_DIR>/36-mp3.bin`
- 버전: 2.1.36 / 로그 build3601
- SHA256: `1a822ba99492e2a68905a47886234b041eec43999fe394684cea83e1942fa81d`
- user code 151,928 B / 전체 이미지 1,969,737 B
- UART0@921600 로그 전용, UART1@115200 공장 브리지 유지. WEC1/PC 오디오 기능은 이 후보에서 비활성.

## 실제 실패와 원인

v35 설치 후 UART0에서 `build=3501 phase=4 init=0 ready=1`을 확인했다. 2026-09-19 22:22경 사용자의 깨우기와 Hello를 인식했고, 내부 명령36→외부ID26 UART 송신까지 진행했다. 그러나 매번 `play start` 직후 `wave fmt err!`와 `receive_data err!`가 발생했으며 사용자는 무반응이라고 답했다.

공장 음원 파티션에는 ID3+CI 헤더의 MP3 음원156개와 메타데이터1개가 있다. v35는 `USE_MP3_DECODER=0`, `AUDIO_PLAY_SUPPT_MP3_PROMPT=0`이라 이 음원을 지원하지 않았다. 파일 보존 검사는 통과했지만, 실행 코드의 형식 지원을 누락한 것이 문제였다. PC PCM 스트리밍 성공을 공장 MP3 응답 성공으로 간주하면 안 된다.

## 수정 범위

위 두 플래그를1로 바꿔 SDK에 이미 있는 MP3 디코더와 CI MP3 프롬프트 판독 경로를 활성화했다. 변경 SDK 파일은 user_config.h와 빌드 표시를3601로 바꾼 we_bridge.c/user_msg_deal.c 세 개뿐이다. v35의10초 소프트웨어 진단은 유지한다.

새 모델/음원 변환/포맷 검사 우회/강제 복구는 없다. 음량 상한40, 핀·코덱·프로토콜·명령표·공장 리소스·파티션/NV 배치도 유지했다. 원본 SDK와 v34/v35/v27/latest 파일은 보존했다.

## 검증

- SDK의 실제 `check_ci_voice_head` 함수와 실제 헤더 타입을 호스트에서 컴파일했다. ROM의 strncmp 함수 포인터만 표준 strncmp로 연결하고 로그 출력은 생략했다.
- 동일한 공장156개 MP3 헤더: 지원off는 전부 거절, on은 전부 modeMP3/head30/정확한 데이터 크기로 인식. 메타데이터는 계속 거절.
- 잘못된 CI 표식 거절, 기존48바이트 CI PCM 형식 유지 확인. 최적화/NDEBUG에 영향받지 않는 검사와 강제 실패 시험으로 검증 코드가 실제 실행되는지도 확인했다. 이는 헤더 판독 회귀이며 실물 MP3 디코딩·스피커 출력 시험은 아니다.
- CI1302 컴파일·링크·공식 code0+libfbin 병합·부트/ASR/DNN/voice/user/NV 배치 보존 검사 통과. 벤더의 기존 빌드 경고는 남아 있다. v35 대비 user code 증가768 B이며 플래시 공간에 맞는다. MP3 활성화에 따른 실제 RAM 사용량은 설치 후 확인한다.
- 근거: `<WE_SDK_TREE_MP3>/header-regression.json`, `fix-verification.json`, `package-3601-8e180d60dd19/verification.json`.

## 설치 후 확인

모듈4핀을 분리한 USB 단독 상태에서 기존 PACK_UPDATE_TOOL 절차로 설치하고 도구를 닫는다. 포트는 재확인한다. 이후 UART0@921600의 build3601/phase4/init0/ready1 및 MP3 메모리 할당 오류 유무를 먼저 확인한다.

Hello Hiwonder→Hello를 말했을 때 `prompt type 3`과 재생 진행, `wave fmt err` 소멸, 마이크 음소거 해제, 실제 대답을 확인한다. 콜백이나 오류 소멸만으로 소리가 났다고 판정하지 않는다. 남은 실패가 있으면 실제 로그로 다음 계층을 구분한다.

로봇4핀 시험은 모듈USB 제거·전원분리 후 연결하고 SERVICE/SAFE를 재확인한다. 외부0x34 ACK, CI1302 UART 수신, 실제 발화는 각각 검증해야 한다. PCM/LLM 중계 완료나 과거 모든 무음 원인의 해결로 확대하지 않는다.
