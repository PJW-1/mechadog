# XIAO ESP32S3 Sense 비전 펌웨어

XIAO는 OV2640 또는 OV3660 영상을 JPEG로 받아 Host PC에 MJPEG로 전달한다. 객체 검출은
수행하지 않는다.

## 업로드

1. `wifi_secrets.example.h`를 같은 폴더의 `wifi_secrets.h`로 복사한다.
2. `MECHDOG_WIFI_SSID`와 `MECHDOG_WIFI_PASSWORD`를 실제 2.4GHz 공유기 값으로 바꾼다.
3. Arduino IDE에서 `firmware_xiao_vision.ino`를 연다.
4. 보드는 **XIAO ESP32S3**, 포트는 연결된 XIAO의 COM 포트를 선택한다.
5. **Tools > PSRAM > OPI PSRAM**을 선택한다. 비활성화하면 카메라 프레임버퍼를 만들 수 없다.
6. 업로드 후 시리얼 모니터를 **115200 baud**로 연다.

AP 모드는 열지 않는다. 공유기 STA 연결만 사용하며, 시리얼 모니터에 아래 세 줄이 나오면
초기화가 끝난 것이다.

```text
CAMERA_READY sensor=OV3660 psram_free=... profile=VGA
WIFI_READY ip=192.168.... rssi=...
HTTP_READY status=http://192.168..../ stream=http://192.168....:81/stream
```

`WARN wifi_connect: status=6`만으로 비밀번호 오류를 단정하지 않는다.
`WIFI_ASSOCIATED`는 AP 연결, `WIFI_DISCONNECTED reason=<숫자>`는 드라이버의
연결 종료 사유다. 사용 중인 ESP-IDF 버전의 reason 정의와 대조한다.
로그는 SSID·비밀번호를 출력하지 않으며 기존 재연결 정책은 변경하지 않는다.

카메라를 본체용 ESP32 코어 환경과 별도로 빌드할 수 있다.
2026-09-12 해당 XIAO에서 코어 2.0.12 후보는 앱 로그를 확인하지 못했고,
별도 설치한 코어 3.3.11과 `esp32:esp32:XIAO_ESP32S3:PSRAM=opi`에서는
OV3660·PSRAM 초기화를 확인했다. 이는 해당 장치 관측이며 SDK만이 원인이라는
확정은 아니다. Wi-Fi·영상 수신 검증은 별도이며 이 관측만으로 완료 처리하지 않는다.

## 실물 검수

- 상태: `http://<XIAO-IP>/`
- VGA: `http://<XIAO-IP>/profile?name=VGA`
- QVGA: `http://<XIAO-IP>/profile?name=QVGA`
- 프레임률 상한: `http://<XIAO-IP>/profile?name=VGA&fps=25` (1~60, 선택 인자)
- 영상: `http://<XIAO-IP>:81/stream`

### 프레임률 상한

`fps` 는 **호스트가 기동 시 `config.yaml` 의 `vision.stream_fps_limit` 값으로 내려보낸다.**
펌웨어 기본값(25)은 폴백이며, 사람이 손으로 바꿀 일은 검수 때뿐이다.

⚠️ **상한의 목적은 낮추는 것이 아니라 예측 가능하게 만드는 것이다.** 상한이 없으면 JPEG
크기가 화면 내용에 따라 변하고 발열에 따라 fps 가 흘러서, 명령 타임아웃(300ms) 대비 최악
부하를 계산할 수 없다. 구현은 **프레임을 잡은 뒤에 버리는** 방식이다 — 먼저 기다렸다가
잡으면 그 사이 시간만큼 낡은 프레임을 보내게 된다.

스트림을 10초 이상 열면 시리얼 모니터에 `STREAM_STATS ... fps=...`가 5초마다 출력된다.
`bytes_avg`·`kbps`·`skipped` 가 함께 나오므로 **대역폭을 추정하지 않고 읽을 수 있다.**
WBS 4.2.2 완료에는 **VGA에서 15fps 이상**이 두 번 연속 확인되어야 한다.

> ⚠️ **스트림은 한 클라이언트만 받는다.** 스트림 핸들러는 연결이 끊길 때까지 반환하지 않으므로
> 두 번째 접속은 대기한다. **브라우저로 검수할 때는 호스트 수신을 끄고**, 반대로 호스트를 돌릴
> 때는 브라우저 탭을 닫는다. 대시보드는 XIAO에 직접 붙지 않고 호스트를 거친다(설계상 그렇다).

2026-09-08 실물 검수에서는 OV3660과 8MB PSRAM을 인식했고, 최종 펌웨어의 VGA 스트림을
11초 동안 304프레임 수신했다. 장치 로그의 두 측정 구간은 각각 27.5fps, 27.8fps였다. QVGA 전환과
VGA 복귀도 상태 API로 확인했다.

`wifi_secrets.h`는 Git에서 제외된다. 실제 공유기 비밀번호를 PR, 로그, 스크린샷에 남기지
않는다.
