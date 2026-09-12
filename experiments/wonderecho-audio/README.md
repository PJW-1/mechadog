> **후속 실물 작업과의 구분:** 이 PR은 18개 합성시험 시점의 초기 스냅샷입니다. 별도 음성 작업의 2026-09-13 인계에서는 코덱 메모리 해제 수정 후 5초/250패킷/FIN/WAV 80,000샘플 성공 1회와 후속 큐 초과 실패, 추가 계측 후보를 보고했습니다. 해당 결과를 이 PR 코드의 성공 근거로 사용하지 않습니다. 후속 수정은 이 스냅샷에 아직 없으므로 아래 초기 후보로 새로 설치하지 말고, 음성 작업의 검증 마감 후 코드를 별도 통합해야 합니다.
> **게시 초안 / 2026-09-13:** 아래는 단계별 개발 이력을 보존한 문서입니다. 현재 후보는 캡처·Speex·UART 송신 및 PC 수신 코드와 전체 시험 이미지 생성까지 준비됐지만 실물 설치·음성 수신·한국어 인식은 미검증입니다. 이전 절의 ‘미연결’ 설명은 해당 시점 기록이며 마지막 절이 최신 상태입니다.
>
> 저장소의 WBS3.8.2/4.7.1/4.7.2는 명령 ID 기반입니다. 사용자 요청의 PC 음성 인식 경로는 이 폴더의 실험이며, 기존 인증·런타임에 자동 연결하거나 기존 DoD를 완료 처리하지 않습니다. SDK/툴체인/공장 BIN은 재배포하지 않습니다. 문서의 C:/dev 경로는 작성자 로컬 예시이고 실제 준비 스크립트의 root 인수로 지정합니다.
# WonderEcho 음성 전송 준비

2026-09-13 Codex. PC에서 한국어를 인식하는 사용자 요청에 맞춘 **전송 해석기 준비 단계**입니다. 펌웨어 설치나 마이크 수신 완료를 뜻하지 않습니다.

## 현재 구현

- `protocol.py`: CI130X LLM AIOT SDK 2.2.7 UART 바이트 패킷의 분할 수신, 길이 제한, 페이로드 합계 검사, 손상 후 재동기화, 미완성 패킷 유휴 시간 초과 처리.
- `test_protocol.py`: 독립적인 hex 예제와 합성 패킷으로 8개 단위시험. 실제 음성 측정 자료가 아닙니다.
- 직렬 포트, 네트워크, 음성 모델, 녹음 및 인증 기능은 아직 연결하지 않았습니다. 이름 발화만으로 인증을 승인하지 않습니다.

실행: 이 폴더에서 `python -m unittest -v test_protocol.py`.

## 확인한 SDK 형식

SDK 원본: `C:/dev/mechadog-voice-20260913/vendor/CI130X_SDK_LLM_AIOT_2.2.7`.
`projects/offline_asr_llm_aiot_uart_sample/app/app_common/cias_common.h`와 `app/app_audio_handle/cias_network_msg_send_task.c` 기준입니다.

헤더는 little-endian `<IHHHHI`, 16바이트입니다. 순서는 magic(0x5a5aa5a5), payload 합계의 하위16비트, type, payload 길이, version, fill_data입니다. 헤더 자체는 체크섬 보호가 없어 모든 손상을 감지할 수 없습니다. 음성 순서 번호 검사는 후속 발화 처리 계층에서 구현해야 합니다.

`PCM_MIDDLE` 명칭만으로 원음이라고 판단하면 안 됩니다. SDK 기본 설정은 Speex이며 Opus/PCM 여부는 실제 펌웨어 설정으로 결정됩니다. 현재 해석기는 payload를 변환하지 않습니다. WonderEcho 공장 I2C 명령 형식과도 다릅니다.

## 빌드·실물 적용 전 남은 확인

1. 공식 Windows GCC 9.2.0 설치 및 기본 샘플 ELF 빌드 완료. 아래 호환성 문제 해결 필요.
2. 공장 복구 바이너리 확보와 CI1302 보드 적합성 확인. SDK 기본 CI1306/4MB 설정을 그대로 설치할 수 없습니다.
3. WonderEcho USB UART 배선과 마이크·클록·출력 전기적 설정 확인. 공식 WonderEcho UART1 115200/open-drain 설정과 샘플 921600/push-pull 설정이 다릅니다.
4. 설정 확정 후 빌드, 제한된 실물 수신 검증, PC 인식 경로 연결 순으로 진행합니다. 현재 SDK 원본은 변경하지 않았습니다.

공식 출처: [SDK 자료실](https://aiplatform.chipintelli.com/attachment), [WonderEcho 펌웨어 개발](https://docs.hiwonder.com/projects/WonderEcho/en/latest/docs/5_Firmware_Development.html).
자료실에는 V3.0.13도 있으므로 확보한 2.2.7을 최신 버전이라고 부르지 않습니다. 새 모델이나 SDK로 자동 교체하지 않았습니다.

## GCC 설치와 빌드 결과 — 2026-09-13

- RAR 원본 96,134,818바이트, SHA256 `12c0eedc8b828e9a063eb84e629ff964b1f83eff45936e8381c60cf0bdcaf45e`. 7-Zip 전체 검사 정상, 사본 해시 일치. 원본 다운로드 보존.
- 도구 위치: `C:/dev/mechadog-voice-20260913/toolchain/gcc_fix_raissrc/bin`, GCC9.2.0 및 GNU Make4.2.1 실행 확인. 전역 PATH 수정 없음.
- 기본 SDK 사본 `build-sdk-2.2.7`: CI1306 reference ELF 컴파일/링크 성공. text117456/data2096/bss141494바이트. 이는 전체 플래시·동적 RAM 사용량이 아닙니다. SDK 자체의 암시적 함수 선언·LTO 타입 불일치 경고가 남아 있으므로 실물 실행 검증을 의미하지 않습니다. 전체 로그 `C:/dev/mechadog-voice-20260913/build-reference.log`.
- `prepare_ci1302.py`는 SDK 원본을 새 후보 폴더에 복사하고 CI1302 reference board, UART0/1 open-drain, UART1 115200, 내부RC, baud calibration OFF 설정을 적용합니다. WonderEcho의 GPIO/mic/PA 매핑을 검증했다는 뜻이 아닙니다. 원본 보존을 위해 목적지가 이미 있으면 실패합니다.
- `build_candidate.ps1`은 후보 ELF만 생성하도록 제한하고 다운로드·포트 조작은 하지 않습니다. 빌드 시도 결과 **실패**: 후보 `user_config.h:577`, `aiot application must be USE_EXTERNAL_CRYSTAL_OSC = 1`. SDK의 명시적 외부 크리스털 요구와 WonderEcho 공식 내부RC 지침이 충돌합니다. 이 조건을 삭제하거나 외부 크리스털이 실장됐다고 가정하지 않았습니다. UART/IIS/HPOUT 샘플 모두 동일한 요구가 있습니다.
- 재현: `python prepare_ci1302.py C:/dev/mechadog-voice-20260913` (최초1회), 이후 `./build_candidate.ps1`. 현재 후보는 위 호환성 오류가 재현되는 검토용이며 설치용 펌웨어가 아닙니다. 로그 `C:/dev/mechadog-voice-20260913/build-ci1302-candidate.log`.

다음 단계는 WonderEcho 회로/호환 SDK의 클록·음성 전송 지원을 확인하는 것입니다. 이 SDK의 즉시 적용 경로가 막힌 것이며 모듈로 음성 전송이 원천 불가능하다고 확정한 것은 아닙니다. 제조사 복구 바이너리 확보와 USB UART 경로 검증도 남아 있습니다. 기기 펌웨어는 변경하지 않았습니다.

## 호환 경로 추가 조사 — 2026-09-13

공식 WonderEcho 배포 자료의 `3. Chip Document & Technical Specifications`에는 CI1302 칩 데이터시트와 2페이지 모듈 사양서가 있습니다. 이 목록에서 WonderEcho 보드 회로도는 찾지 못했습니다. 모듈 사양서는 4핀 I2C 연결을 명시하며, 펌웨어 개발 문서는 UART1을 내부 명령 통신에 사용합니다. **외부 I2C 단자를 UART TX/RX라고 안내하면 안 됩니다.** USB-C의 런타임 UART 연결은 회로 수준에서 미확인입니다.

CI1302 데이터시트 V1.2 p11 Note2는 내부RC에서 UART를 최대115200bps로 제한합니다. 따라서 클록 설정만 무시하고921600으로 바꾸는 방법은 채택하지 않았습니다. SDK 하위 codec driver에는 내부RC 분기가 있지만, 이것만으로 AIOT 전체의 클록 guard를 삭제할 근거가 되지는 않습니다.

전송량 계산(실물 측정 아님, UART8N1·16kHz·mono·16bit 기준):

- 원음 32000B/s, SDK128B 분할마다16B헤더면36000B/s → 최소360000bps. 115200에서 실시간 전송 불가.
- SDK 기본 Speex43B+헤더16B를20ms마다 전송하면2950B/s →29500bps, 115200의약25.6%. 오디오 업로드만의 이론값이며 추가 제어/디버그/하향음성/실제 클록 오차는 별도입니다. 대역폭상 후보일 뿐 보드 동작 보장이 아닙니다.

PC PnP 읽기 확인: COM5 CH340 정상. 별도로 나타나는 USB Audio 입력(VID0BDA/PID5856/MI02)은 같은 USB composite의 HD WEB CAMERA(MI00)에 속합니다. 이 입력을 WonderEcho 마이크로 사용하거나 인식 성공 증거로 삼지 않았습니다. 녹음/포트 열기 없음.

공식 복구 파일은 Google Drive connector로 원본 raw fetch 성공(1969737B). 파일 이름 `2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin`, [원본 파일](https://drive.google.com/file/d/1Uh2198JLjOuEM_Al_J4NuDmCzP3pokXP/view). 반환물은 서비스 파일 참조이고 로컬 materialize 수단이 확인되지 않아 **PC 로컬 사본·SHA256·복원 시험은 아직 미완료**입니다. 브라우저 다운로드 버튼만으로는 Downloads에 파일이 생성되지 않았습니다.

현재 필요한 추가 근거는 실물 모듈의 앞뒤 부품/보드 버전 확인과 정확한 회로·클록 지원입니다. 하드웨어 수정, 대체 마이크, 인식 모델 교체, 로봇 펌웨어 변경은 하지 않았습니다. 가능한 후보는 내부RC를 공식 지원하는 기반 위에서 압축 음성을 송신하는 방식이며, 현재 확보한 AIOT2.2.7의 지원 경로로 검증되지는 않았습니다.

출처: [Hiwonder 기술자료 폴더](https://drive.google.com/drive/folders/1LbhDvHzAvqX2EY0ybEk_zSDEJXRg36pt), [CI1302 데이터시트](https://drive.google.com/file/d/1ifNC_Cs1_cyyrjLxP_hURHKNv9UiOjKv/view), SDK의 `cias_voice_upload.c`, `cias_network_msg_send_task.c`, `driver/boards/CI-D02GS01J.c`, `components/codec_manager/codec_manager.c`.

### 실물사진·수신 확인 추가
2026-09-13 사용자 사진에서 Hiwonder Voice Module V3.0, SCL/SDA/GND/5V 및 MIC/RST/PWR/STA 실크 확인. 사진의 칩글씨/배선만으로 외부크리스털·USB UART루트를 확정하지 않음. COM5 CH340115200에서6.047초41바이트 수신, 정상로그/알려진패킷 미확인. 명령송신0,명시적리셋펄스없음. 실물측정폴더02에사진2장과수신원본보관. 펌웨어변경·음성인식성공아님. 추가사진반복요청 대신 내부RC지원기반SDK/정확회로근거가 필요한 상태.

## Offline 내부 RC·압축 어댑터 빌드 — 2026-09-13

사용자가 받은 `CI130X_SDK_ASR_Offline_V1.12.16.zip`은 59,355,684바이트입니다. 7-Zip 전체 검사(623파일) 정상, 사본 SHA256은 `cd00b3d6e82ba81bf751a7e21d0eca99f8353142b72e55fda0276cb45cdf7aff`로 원본과 일치합니다. 원본은 보존했습니다.

`prepare_offline.py`는 별도 SDK 사본에 CI1302, 내부 RC, UART0/1 open-drain, UART1 115200을 설정합니다. baud calibration은 원래 OFF 설정을 유지합니다. CI-D02GS01J는 컴파일용 기준 보드이며 WonderEcho의 전체 회로를 확인했다는 의미가 아닙니다. 기본 ELF 빌드 성공: text 86,426 / data 764 / bss 72,136바이트. 앞서 실패한 AIOT SDK의 외부 크리스털 guard는 수정하지 않았습니다.

`voice_encoder.c`/`.h`는 16kHz 단일 채널 PCM 320샘플(20ms)을 SDK의 Speex 정수 인코더로 압축하고 기존 AIOT 규격의 16바이트 헤더·순번·payload 합계 체크섬·길이 바이트를 붙입니다. 출력 버퍼 최소144바이트, 입력 길이·압축 결과 크기 검사 및 오류 반환을 구현했습니다. 샘플을 일부 잘라 전송하지 않습니다. 한 작업에서만 호출해야 합니다. 코덱 내부 동적 할당과 CPU 사용량은 실제 기기에서 검증해야 합니다.

`prepare_offline_encoder.py`는 기본 사본을 별도 `offline-codec-1.12.16`에 복사하고 이미 확보한 AIOT2.2.7의 Speex 라이브러리/헤더(원본 보존)와 어댑터를 연결합니다. 라이브러리 SHA256 `ecf5034e31110c16f4bb7c1d0337d3b1e6e44ea8402a54506acd0a4f0d71eebe`. 새로운 인식 모델을 넣은 것이 아닙니다. SDK에서 VBR/VAD/DTX 제어가 컴파일 제외된 구성을 반영했고, float 왕복 변환 없이 정수 입력 API를 사용합니다.

검증:
- 어댑터 RISC-V `-Wall -Wextra -Werror` 컴파일 통과.
- 코덱 포함 ELF 링크 성공. text 117,718 / data 812 / bss 72,948바이트. `we_encoder_init`, `we_encode_frame`, `we_encoder_close`, `sb_encoder_init` 심볼 존재 확인. 링크 제외로 성공한 것이 아닙니다.
- PC 패킷 해석기 기존 합성 입력 시험 8개 통과. 실제 Speex 인코딩 실행이나 마이크 시험이 아닙니다.
- SDK 본체에는 기존 암시적 함수 선언과 `load_freq_correct_factor`/`efuse_check_idle` 타입 경고 등이 남습니다. 어댑터 무경고가 전체 SDK 무경고를 의미하지 않습니다.

재현(최초 준비는 기존 목적지가 있으면 중단):
```powershell
python prepare_offline.py C:/dev/mechadog-voice-20260913
./build_offline.ps1
python prepare_offline_encoder.py C:/dev/mechadog-voice-20260913
./build_offline.ps1 -WithEncoder
```

**현재 산출물은 설치용이 아닙니다.** 코덱 어댑터는 시작 코드에 연결하지 않았으며 마이크 캡처·UART 송신도 아직 연결하지 않았습니다. 따라서 실시간 음성 수신·한국어 인식 완료가 아닙니다. 남은 일은 보드의 USB UART 경로/마이크·앰프 설정 확인, 공장 복구 파일 로컬 검증, 캡처/송신 작업 연결 및 실물 CPU·메모리·음성 시험입니다. GPIO·COM·펌웨어·로봇 본체 조작은 이번 작업에서 하지 않았습니다.

빌드 로그: `C:/dev/mechadog-voice-20260913/offline-ci1302-build.log`, `offline-codec-build.log`. 사본별 manifest에 출처·해시·미검증 사항을 기록했습니다. ELF 크기는 동적 메모리나 전체 패키지 플래시 크기를 포함하지 않습니다.

## 캡처·송신 연결 후보와 PC 수신기 — 2026-09-13

이번 후보는 `voice_stream.c/.h`로 마이크 처리 출력 → 8프레임 큐 → Speex 인코더 → UART0 송신을 연결했습니다. `prepare_stream.py`가 별도 `offline-stream-1.12.16` 사본에 적용합니다. 기존 두 후보와 원본 SDK는 유지합니다.

- SDK 기본 ADC는32kHz이고 내부 처리 결과 DST1은16kHz입니다. `ci_ssp_config.c`에서 DSP 출력 콜백만 켜고 `audio_pre_rslt_write_data`의 right(DST1)256샘플을 즉시 복사합니다. 기존32→16kHz 처리를 유지하며 외부 IIS 핀 출력은OFF입니다. VAD 표시용 샘플 변형도OFF입니다. 이 경로는 소스/설정에서 확인했으며 실제 콜백 주기·파형은 미측정입니다.
- UART0는 **USB 연결 경로를 실물 검증하기 전의 후보 선택**입니다. 본체 UART와 혼용하지 않습니다. 로그/기존 명령 UART/재생기 설정을OFF로 두어 전용으로 사용합니다. GPIO/앰프 설정 전체가 검증된 것은 아닙니다.
- 기본은 전송OFF. Host QUERY(0x0102) → WEC1 READY 확인 → START(0x0108) → 최대250프레임(5초) → FINISHED 응답입니다. STOP은0x0106입니다. 명령은 기존 AIOT16바이트 헤더/빈payload 형식이며 이 시작·정지 의미는 **우리 시험 후보의 프로토콜 확장**입니다. 공장 펌웨어 프로토콜이 아닙니다.
- 상태 payload는 `<4sHHII>`: WEC1/phase/reason/전송프레임수/16000. phase1준비,2시작,3종료,4실패. reason0정상,1중지,2시험길이도달,3큐초과,4코덱실패,5UART시간초과,6입력시간초과,7입력크기오류입니다.
- 오디오 복사는 제한된 크기로 수행하고 압축/송신은 별도 작업에서 처리합니다. FIFO 공간은 인터럽트/세마포어로 기다리며 송신40ms, 입력중단1초, 세션7초 기한을 둡니다. 큐가 넘치면 시험 실패로 종료합니다. 프레임을 버린 뒤 정상 완료로 보고하지 않습니다.

`stream_client.py`는 WEC1 장치 식별 후5초를 요청하고 체크섬/연속순번/43바이트 Speex payload/최종250프레임을 검증합니다. 완료된 데이터만 PyAV로 디코딩해16kHz mono PCM WAV를 생성합니다. raw UART와결과JSON을 함께 보존하고 기존 출력 폴더는 덮어쓰지 않습니다. 아직 실제 COM 포트에서 실행하지 않았으며 WAV를 생성한 실음성 성공 사례도 없습니다. 식별은 프로토콜 확인이며 암호학적 인증이 아닙니다.

PC 환경은 `C:/dev/mechadog-voice-20260913/pc-tools`에 별도 설치했습니다. `requirements-pc.txt`: av18.1.0,pyserial3.5. 다른 프로젝트 환경 변경 없음. 마이크·Speex 실제 실행 시험과 구분되는 합성 프로토콜/제어 시험18개 및 PC Speex 디코더 초기화 통과.

빌드/패키지:
- 새 encoder/stream C 코드 모두 -Wall -Wextra -Werror 컴파일 통과. 전체SDK에는 기존 경고가 남습니다.
- 연결된 ELF: text101718/data472/bss71084바이트. 동적힙/스택 여유 및CPU는 실물에서 검증해야 합니다.
- `package_stream.ps1`: 제조사 ci-tool-kit의 user-file 병합으로 `[0]code.bin`102208B + `[1]code.bin`64264B → `user_code.bin`166504B. 기존 인식모델·음성·사용자리소스는 새로 생성하지 않았습니다.
- `C:/dev/mechadog-voice-20260913/offline-stream-package/user_code/user_code.bin`은 **전체 공장 이미지가 아니며 아직 설치용으로 검증되지 않은 후보**입니다. `NOT_FLASH_READY.txt` 및해시파일 동봉. 최종 ELF에서 다시 변환한 바이너리와 패키지의코드부분 해시 일치 확인.

공장 복구 파일은 이번에 Chrome의 공식Drive 다운로드로 로컬 확보했습니다. 1969737B, SHA256 `c4328480d8f9cbe2e15dde41bb7d170d93c46c96c884742394322db87b5d2b2d`. Downloads 원본과 development/recovery 사본 일치. [제조사 절차](https://docs.hiwonder.com/projects/WonderEcho/en/latest/docs/5_Firmware_Development.html), [원본 파일](https://drive.google.com/file/d/1Uh2198JLjOuEM_Al_J4NuDmCzP3pokXP/view).

`inspect_factory.py`는 SDK의 packed partition_table_t(0x2000,278바이트)와 상태 바이트 정규화 규칙으로 원본 테이블 체크섬12089/12089를 확인했습니다. CI1302/format2, 코드영역16384~다음ASR184320으로 물리 공간167936B, 후보166504B가 들어갑니다. **물리 크기 적합성은 모델 ABI/업데이터/실물 호환성 증명이 아닙니다.** 원본 코드길이는166232B이며 단순 바이트 덮어쓰기를 해서는 안 됩니다. 공장 이미지나 원본 파티션 표는 수정하지 않았습니다. 상세는 offline-stream-package/factory-compatibility.json.

재현:
```powershell
python prepare_stream.py C:/dev/mechadog-voice-20260913
./build_offline.ps1 -WithStream
./package_stream.ps1
C:/dev/mechadog-voice-20260913/pc-tools/Scripts/python.exe -m unittest discover -s . -p 'test_*.py'
```
실물 수신기는 설치·포트 확인 후에만 `stream_client.py --port <음성모듈COM> --out <새측정폴더>`로 실행합니다. 현재 후보가 설치되지 않은 공장 상태에서는 WEC1 응답이 없어 시작하지 않습니다. 실제 사용 시 생성되는 측정 폴더의 목차/측정기록/일지 갱신은 프로젝트 측정 보관 규칙을 따릅니다.

남음: WonderEcho의 USB UART0/마이크·앰프 매핑, 기존 리소스와 새SDK 호환 및 공식 업데이터 방식 확인 → 설치 →5초실음성/CPU/메모리 검증. 아직Wi-Fi전송·PC한국어인식·신원대답 기능을 완성한 단계는 아닙니다. 로봇본체·COM·실물마이크·플래시 조작 없음.

## 초기화 수정·업데이터 표시 후속 — 2026-09-13
`fix_sdk_startup.py`는 private stream SDK에 float load_freq_correct_factor(void) 선언을 추가하고, memset 크기 변환과 flash 보정값의 비정렬/별칭 포인터 읽기를 memcpy로 수정합니다. 보정 범위와 기본값은 그대로입니다. 원본 vendor와 수정 전 파일 사본 보존. prepare_stream.py 신규 준비에도 적용합니다. startup-fix-manifest.json이 이 세 파일의 후속 해시 정본이며 이전 stream-manifest의 동일 경로 해시를 대체합니다.
재빌드 ELF text101654/data472/bss71084, load_freq_correct_factor LTO 경고 제거. efuse 및 다른 기존 SDK 경고는 남습니다. 별도 offline-stream-package-startupfix/user_code/user_code.bin166440B(기존 패키지 유지). 실제 보드에서 실행한 검증은 아닙니다.
공식 PACK_UPDATE_TOOL V3.8.4는 표시 모드 실행과 활성화로 메인 화면 확인. 기본 CI110X 상태라 설치 전 CI1302 선택 필수. 영어 버튼 입력은 SendInput GetLastError87, 다음 확인에서 물리 Esc 중단으로 추가 조작 안 함. COM 선택/펌웨어 쓰기 없음. 전체 패키지 호환성 및 실물 음성 검증은 계속 남아 있습니다.

## 전체 시험 이미지·앰프 핀 제외 — 2026-09-13
`prepare_stream.capture_only_board`는 마이크 등록 시 PC4 앰프 핀을 자동 구동하는 reference 호출을 제거합니다. 수신 전용 시험에 불필요한 출력 핀을 건드리지 않기 위한 변경입니다. `capture-only-board.json`이 해당 보드 파일의 후속 해시입니다. 최종 ELF101462/472/71084, 사용자코드166248B.
`build_full_candidate.py`는 공식 mf 명령으로 전체 BIN을 만듭니다. 원본 복구 SHA 고정 검사 및 부트8KB/각 자원 SHA/주소/길이/버전/NV 위치·크기를 결과 이미지와 대조합니다. 기존 ASR모델을 새로 도입하거나 바꾸지 않습니다. 실제 SDK 모델 ABI/마이크 배선/압축 실행의 검증을 대체하지 않습니다.
최종 시험 후보: C:/dev/mechadog-voice-20260913/full-stream-bench-v2/image/WonderEcho_Stream_Bench_2.0.1.bin
SHA256: 1619a4862b85ff38da081c612023c6e33f35548f32d04c29d73aa76821a6e5b1
복구원본: C:/dev/mechadog-voice-20260913/recovery/2.2 CI1302_English_SingleMic_V00729_UART1_115200_2M.bin
생성 재현: build_full_candidate.py <root> <새출력폴더> --code-package offline-stream-package-captureonly
PC합성18시험 통과. 실음성/설치검증 아님.
공식 CLI code_program.exe는 두 SDK 모두 Support Device 인자1301로 확인되어 CI1302에서 실행하지 않았습니다. inspect_vendor_cli.py는 pefile/capstone을 사용하는 읽기 분석기이며 포트를 열지 않습니다.
GUI는 CI1302 Update 화면까지 도달했습니다. 후보 선택 전 기본 경로 파일 열기 실패, COM5/921600/EraseNV체크 기본값 상태. 파일선택조작에서 사용자 물리Escape 중단 보고. 아직 firmware write 없음. 설치 전115200과EraseNV해제 및후보파일확인 필요. RST는업데이트가실제로준비된뒤에만 요청합니다.
