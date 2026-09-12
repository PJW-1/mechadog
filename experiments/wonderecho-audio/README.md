# WonderEcho 음성 전송·로컬 인식 개발 초안

PR #83의 초기 스냅샷에 녹음 종료 처리·진단·반복 안정화와 PC 한국어 인식 후속 소스를 추가했다. **현재 안내 재생은 별도 실물 검증 중인 개발 후보다. 이 폴더 전체를 v5 실물 검증 완료 펌웨어로 취급하거나 그대로 설치하지 않는다.**

## 검증된 범위와 개발 중인 범위

- 기존 v2는250프레임을 받았지만 종료 메시지가 없어 실패했다. `fix_codec_cleanup.py`는 Speex NB 할당/해제 짝과 SB 중복 해제를 수정한다. v3 첫 정상종료 후 반복 큐초과, v4 계측에서는 인코딩 최대52ms(선점 포함)가 관측됐다.
- v5는 녹음 워커 우선순위3→4 변경 후 기존 별도 실물 작업에서5초녹음5회 각250프레임/FIN/오류0을 확인했다. 최대 인코딩10~18ms·큐1/8이었다. 장시간·여러화자 검증은 아니다. 이 수치는 원래 v5 설치본의 측정이며 아래 재생 후보의 측정이 아니다.
- `transcribe_local.py`는 완료된5초16kHz PCM16 캡처만 로컬 PC의 승인된 faster-whisper medium/CUDA 모델로 처리한다. 말한 이름 추출은 신원 인증이 아니다. 텍스트를 실행 명령으로 쓰지 않는다. 기존 작업의 한국어 인식 실측이 있으며 모델 로딩·녹음 시간과 추론 시간을 구분한다.
- `voice_prompt.*`, `prepare_speaker.py`, `build_speaker_image.py`와 `--prompt`는 새 안내 재생 후보다. 패키지 구조의 오프라인 시험과 원래 v5 녹음 성공을 구분한다. 이 게시 작업은 안내 음성 출력·재생/녹음 전환을 기기에서 검증하지 않았다.
- 게시 작업은 실행 중인 원본 개발 폴더를 변경하지 않고 파일 내용이 안정적인 시점의 복사본을 만들었다. 이후 원본 변경은 자동 반영되지 않는다. SDK·도구 바이너리·모델·음원·실측 원본은 포함하지 않았다.

## 구성과 재현

- `protocol.py`, `stream_client.py`: WEC1 프레임·길이·체크섬·순번·종료·진단 해석, USB 제한시간 캡처. 연속250프레임과 정상 종료가 없으면 성공으로 표시하지 않는다.
- `voice_stream.*`, `voice_encoder.*`: 장치의 마이크 수집/압축/전송 어댑터. AI 음성 인식은 Host PC에서 실행한다.
- `prepare_*.py`, `fix_*.py`: 사용자가 별도로 확보한 호환 SDK 사본을 준비한다. 원본 SDK와 공장 복구 이미지를 보존하고 확인한 입력 해시에만 적용한다.
- `build_*.ps1`, `package_stream.ps1`: `-DevelopmentRoot`를 명시해야 한다. 호환 Offline SDK1.12.16/GCC9.2.0, CI1302 보드·클록·핀·리소스를 검증한 개발환경이 선행이다. 초기2.2.7 후보는 클록 호환 실패를 재현하는 기록용이며 설치 경로가 아니다.
- `requirements-pc.txt`는 수신/디코드, `requirements-stt.txt`는 승인 모델을 사용하는 선택적 PC 인식 환경이다. `requirements-pc-tested.txt`는 기존 Windows 시험 환경의 버전 기록이다. 모델은 자동 다운로드하지 않고 로컬 파일만 연다.

```powershell
python -m pip install -r experiments/wonderecho-audio/requirements-pc.txt
python -m unittest discover -s experiments/wonderecho-audio -p 'test_*.py'
# 기기 연결·적합성이 확인된 환경에서만 실행한다. 출력은 Git 밖의 개인 폴더다.
./experiments/wonderecho-audio/recognize_voice.ps1 -DevelopmentRoot D:/voice-private -OutputRoot D:/measurements -Port COM99
```

위 COM99는 예시다. 본체나 다른 모듈 포트를 사용하지 않는다. `recognize_voice.ps1`의 기본은 안내 재생 없이 녹음한다. `-Prompt`는 해당 후보가 실제 설치·검증된 경우에만 사용한다. 공개 저장소의 파일을 내려받았다는 사실은 빌드·플래시 준비 완료를 뜻하지 않는다.

현재 WBS3.5.5/4.7 계열의 개발 근거이며 공장 문구/ID 방식의 전체 완료 판정은 하지 않는다. 무선 중계·상시 서비스·사원 DB 대조·장시간 검증은 남아 있다. 본체/카메라 기능을 바꾸지 않는다.
