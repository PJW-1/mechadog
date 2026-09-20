# WonderEcho v37/v38 — 출력단 계측 + PDM 전원 수정 (최신 후보)

상태: 빌드·패키징 완료. **실물 미검증** — `38-pdm.bin` 설치 후 청취 확인이 필요하다.

## 개요

- v37 (`37-output-diag.bin`): v36(mp3-fix)에 출력단 계측 추가. `gain/vol/pa_req/decoded/irq/tx_peak` 를 `[WE-STAT]` 로그로 10초 주기 출력. 디지털 완주(디코드·DMA·IRQ 증가)가 확인됐지만 물리 무음.
- v38 (`38-pdm.bin`): v37 + **PDM 블록 부팅 전원 시퀀스**. 로그 빌드에서 `voice_stream.c`/`voice_prompt.c`/`audio_pre_rslt_out_codec_init()` 경로가 전부 빠져 PDM(아날로그 HPOUT 드라이버) 전원이 한 번도 인가되지 않던 결함 수정 — `main.c` 부팅 경로에 `scu_set_device_gate/reset(PDM)` + `pdm_power_up(PDM_CURRENT_128I)` + `pdm_hpout_mute_disable()` 추가.
- 빌드 식별자: UART 로그 `[WE-BRIDGE] build=3801`, `[WE-STAT] build=3801`.
- 이미지: 전체 1,969,737B, 팩토리 파티션 오프셋(asr/dnn/voice/user) 일치. SHA256 `83dc8daf…`.

## v38-src/ — 실제 빌드된 소스 스냅샷

로그 빌드 계열(v34~v38)은 상위 폴더의 스트림 계열 전체 소스(`main.c` 1266줄 WEC1 포함)와 **다른 최소화 계열**이다. 여기 있는 파일이 `38-pdm.bin`에 실제로 들어간 상태다.

| 파일 | 변경 |
|---|---|
| `main.c` | PDM 부팅 전원 추가(line ~176) — 최소화 로그 빌드용 279줄 |
| `user_config.h` | `USE_MP3_DECODER 1`, `AUDIO_PLAY_SUPPT_MP3_PROMPT 1`, `USE_ALC_AUTO_SWITCH_MODULE 0`, `CONFIG_CI_LOG_UART HAL_UART0_BASE` |
| `user_msg_deal.c` | 브리지 init 호출 + `init_result` 로그 |
| `we_bridge.c/h` | `build=3801` 배너 + `[WE-STAT]` 주기 상태 로그 + 출력 계측 |

## 검증 상태

- v34 브리지: PC 네이티브 테스트 + CI1302 컴파일 + 패키지 검증 완료, 로봇 버스 0x34 ACK 실측
- v37 계측: ASR→UART TX, play 완주, IRQ/피크 증가 실측
- v38 PDM 수정: **실물 미검증** — 소리 청취가 첫 번째 확인 항목
- 로봇 `voice_dispatch` 방향 매핑 정정(`robot-mapping.patch`)은 로봇에 **미적용**

## 설치 파일 위치

`*.bin` 이미지는 저장소에 올리지 않는다(`.gitignore` + 벤더 재배포 정책).
팀 공유 위치 또는 TESTING.md 의 배포 안내를 본다.
