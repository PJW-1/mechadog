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

## 실물 검수

- 상태: `http://<XIAO-IP>/`
- VGA: `http://<XIAO-IP>/profile?name=VGA`
- QVGA: `http://<XIAO-IP>/profile?name=QVGA`
- 영상: `http://<XIAO-IP>:81/stream`

스트림을 10초 이상 열면 시리얼 모니터에 `STREAM_STATS ... fps=...`가 5초마다 출력된다.
WBS 4.2.2 완료에는 **VGA에서 15fps 이상**이 두 번 연속 확인되어야 한다.

2026-09-08 실물 검수에서는 OV3660과 8MB PSRAM을 인식했고, 최종 펌웨어의 VGA 스트림을
11초 동안 304프레임 수신했다. 장치 로그의 두 측정 구간은 각각 27.5fps, 27.8fps였다. QVGA 전환과
VGA 복귀도 상태 API로 확인했다.

`wifi_secrets.h`는 Git에서 제외된다. 실제 공유기 비밀번호를 PR, 로그, 스크린샷에 남기지
않는다.
