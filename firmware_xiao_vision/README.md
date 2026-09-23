# XIAO ESP32S3 Sense 비전 펌웨어

XIAO는 OV2640 또는 OV3660 영상을 JPEG로 받아 Host PC에 MJPEG로 전달한다. 객체 검출은
수행하지 않는다. 확장보드의 PDM 마이크 소리도 포트 82로 그대로 보낸다. 음성 인식 역시
Host PC가 맡는다([음성 스트림](#음성-스트림)).

## 업로드

1. `wifi_secrets.example.h`를 같은 폴더의 `wifi_secrets.h`로 복사한다.
2. `MECHDOG_WIFI_SSID`와 `MECHDOG_WIFI_PASSWORD`를 실제 2.4GHz 공유기 값으로 바꾼다.
3. Arduino IDE에서 `firmware_xiao_vision.ino`를 연다.
4. 보드는 **XIAO ESP32S3**, 포트는 연결된 XIAO의 COM 포트를 선택한다.
5. **Tools > PSRAM > OPI PSRAM**을 선택한다. 비활성화하면 카메라 프레임버퍼를 만들 수 없다.
6. 업로드 후 시리얼 모니터를 **115200 baud**로 연다.

AP 모드는 열지 않는다. 공유기 STA 연결만 사용하며, 시리얼 모니터에 아래 네 줄이 나오면
초기화가 끝난 것이다.

```text
CAMERA_READY sensor=OV3660 psram_free=... profile=VGA
MIC_READY rate=16000 dma_ms=256
WIFI_READY ip=192.168.... rssi=...
HTTP_READY status=http://192.168..../ stream=http://192.168....:81/stream audio=http://192.168....:82/audio
```

`MIC_READY` 대신 `ERROR mic_init: i2s driver` 가 나오면 마이크만 못 쓰는 상태다. 영상은
그대로 나가고 `/audio` 만 503 으로 답한다.

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

PC 모델 처리 경계, 재접속 수정, 전송 계측과 독립 검증 명령은
[연결 검증 기록](LINK_VALIDATION.md)을 참고한다. PC 5GHz/카메라 2.4GHz를 같은 LAN에서
사용하는지 확인하며, USB 업데이트 완료와 영상 속도 검증은 구분한다.

- 상태: `http://<XIAO-IP>/`
- VGA: `http://<XIAO-IP>/profile?name=VGA`
- QVGA: `http://<XIAO-IP>/profile?name=QVGA`
- 프레임률 상한: `http://<XIAO-IP>/profile?name=VGA&fps=25` (1~60, 선택 인자)
- 장착 방향: `http://<XIAO-IP>/orient?rot=180` — 모듈을 광축 기준으로 180° 뒤집어 달았을 때 호출. `rot=0`으로 되돌린다. 센서 레지스터(vflip+hmirror)로 바로잡으므로 재플래시 없이 다음 프레임부터 적용된다. 재부팅하면 기본값(0)으로 돌아간다.
- 영상: `http://<XIAO-IP>:81/stream`
- 음성: `http://<XIAO-IP>:82/audio` — 16 kHz 모노 PCM16LE, 이득 `?gain=0..4`(기본 2). 아래 [음성 스트림](#음성-스트림) 참고

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
>
> 클라이언트 입장에서 이 "대기"는 **죽은 스트림과 구분되지 않는다** — 한 장도 못 받고
> 타임아웃으로 끝난다. 2026-09-14 실측에서 나중에 붙은 쪽은 2회 모두 `0 프레임 · TimeoutError`
> 였고 먼저 붙은 쪽은 fps 가 그대로였다([측정](../docs/measurements/2026-09-14-camera-frame.md)).
> 화면이 검을 때 카메라 고장이나 네트워크부터 의심하지 말고 **다른 수신자가 붙어 있는지 먼저 본다.**

2026-09-08 실물 검수에서는 OV3660과 8MB PSRAM을 인식했고, 최종 펌웨어의 VGA 스트림을
11초 동안 304프레임 수신했다. 장치 로그의 두 측정 구간은 각각 27.5fps, 27.8fps였다. QVGA 전환과
VGA 복귀도 상태 API로 확인했다.

## 음성 스트림

포트 82의 `/audio`는 확장보드 PDM 마이크(CLK=GPIO42, DATA=GPIO41)를 16 kHz 16-bit 모노로 읽어
HTTP chunked로 끊지 않고 보낸다. 응답 형식은 `audio/L16;rate=16000;channels=1`이고, 본문은
WAV 헤더가 없는 PCM16LE 샘플이다. DC는 1차 고역 통과 필터로 지우고, 이득은 왼쪽 시프트
`?gain=0..4`(기본 2, ×4)로 준다. 범위를 넘는 값은 0~4로 잘린다.

이 포트는 음성 경로의 **듣기** 입구다. PC가 받아 Whisper로 인식하고 규칙으로 판단한다
([ADR-38](../docs/DECISIONS.md#adr-38)). ⚠️ **호스트 음성 파이프라인은 아직 이 스트림을 받지
않는다.** 현재는 WonderEcho USB 임시 링크를 쓰며, 입력 교체는 WBS 4.7.19(미착수)다.

받는 법:

```bash
# 5초 받아 원시 PCM으로 저장한 뒤 WAV로 바꾼다 (--max-time 으로 끊으므로 curl 종료 코드는 28)
curl -sN --max-time 5 "http://<XIAO-IP>:82/audio?gain=2" -o mic.raw
ffmpeg -f s16le -ar 16000 -ac 1 -i mic.raw mic.wav
```

```python
import urllib.request, wave
with urllib.request.urlopen("http://<XIAO-IP>:82/audio", timeout=5) as r, wave.open("mic.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(r.read(16000 * 2 * 5))  # 5초
```

시리얼 로그:

| 로그 | 뜻 |
| :--- | :--- |
| `MIC_READY rate=16000 dma_ms=256` | 부팅 때 I2S PDM 드라이버를 올리고 멈춰 둔 상태. `dma_ms`는 DMA 버퍼 여유(8 × 512 샘플)로, 전송이 이보다 오래 막히면 샘플이 버려진다 |
| `AUDIO_OPEN gain=<n>` | 클라이언트가 붙어 녹음을 시작했다. 처음 약 100 ms는 PDM 필터가 안정되는 구간이라 버린다 |
| `AUDIO_STATS kbps= rate= overflows= send_max_us= rms=` | 5초마다 출력. `rate`는 초당 샘플 수(16,000 근처가 정상), `overflows`는 DMA 넘침(`I2S_EVENT_RX_Q_OVF`) 횟수로 **0이 아니면 소리가 빠진 것**, `send_max_us`는 가장 오래 걸린 한 덩어리 전송 시간(256,000 µs에 가까우면 넘침 직전), `rms`는 이득을 준 뒤의 음량 |
| `AUDIO_CLOSED result=0x...` | 클라이언트가 끊겨 I2S를 다시 멈췄다 |

> ⚠️ **음성도 한 클라이언트만 받는다.** 영상과 같은 이유로 두 번째 접속은 대기하며,
> 클라이언트 쪽에서는 죽은 스트림과 구분되지 않는다. 영상(81)과 음성(82)은 서버가 따로라
> 서로 막지 않는다. 마이크 초기화가 실패했으면 `/audio`는 `503`과
> `{"ok":false,"error":"mic unavailable"}`로 답한다.

2026-09-23 실측 요약([측정](../docs/measurements/2026-09-23-xiao-mic.md)):

- **마이크 단독(USB, `mic_probe`)**: PC faster-whisper `medium`으로 **6/6** 인식했다. 30 cm와 1 m에서
  이름 「김민수」까지 정확했고 무음에서 문장을 지어내지 않았다.
- **영상과 동시(Wi-Fi)**: 모든 구간에서 음성 샘플률 15,925–16,068, 누락 0, DMA 넘침 0이었다.
  가장 긴 전송 정지는 218 ms로 DMA 여유 256 ms 안이다. 연속 발화 **4/4**를 인식했다.

알려진 한계:

- ⚠️ **영상은 음성과 무관하게 약 2.5 Mbps에서 막힌다.** fps는 "전송량 ÷ 프레임 크기"로 정해지며,
  복잡한 장면에서 프레임이 31 KB로 커지면 **영상만으로도 9.4 fps**까지 내려갔다. 위의 VGA 15fps
  기준(WBS 4.2.2)은 음성과 별개로 재측정이 필요하다.
- 조용한 실내에서만 쟀다. 로봇 탑재(서보 소음), 1 m 초과 거리, 공장 소음은 시험하지 않았다.
  1 m에서는 rms가 무음과 가까웠다.

### 진단 스케치 — `diagnostics/mic_probe/`

카메라·Wi-Fi를 켜지 않고 마이크만 보는 USB 시험 스케치다. 줄 단위 명령 `rec <ms>`(100~10000)로
녹음하면 `STAT rms= peak= dc=` 줄을 먼저 찍고 `DATA <bytes>` 뒤에 PCM16LE 원시 바이트를 보낸다.
`gain <n>`(0~4)은 위 `?gain`과 같은 뜻이다. 보드 설정은 비전 펌웨어와 같다
(`esp32:esp32:XIAO_ESP32S3:PSRAM=opi`). ⚠️ **올리면 비전 펌웨어가 덮어써진다.** 앱 영역을
먼저 백업하고, 시험이 끝나면 되돌린 뒤 되읽기 해시로 확인한다.

`wifi_secrets.h`는 Git에서 제외된다. 실제 공유기 비밀번호를 PR, 로그, 스크린샷에 남기지
않는다.
