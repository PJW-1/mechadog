# XIAO 코어 버전과 스트림 재측정 — 2026-09-25

**`esp32:esp32` 코어를 최신(3.3.11)으로 올리면 마이크가 죽는다.** 같은 소스·같은 기체에서
2.0.12 빌드는 `/audio` 가 정상이고 3.3.11 빌드는 0 바이트다. **컴파일은 양쪽 다 통과한다.**
그래서 CI 만으로는 드러나지 않는다 — `ci.yml` 의 XIAO 코어를 2.0.12 로 고정했다.

같은 플래시에서 `4.2.2` 의 «재측정 필요» 도 해소했다. VGA **31.3 fps · 5,333 kbps** 로
15 fps 기준을 넘는다.

| 항목 | 값 |
| --- | --- |
| 기기 | XIAO ESP32S3 Sense (`68:ee:8f:4f:54:b0`, OV3660, 8MB) · Wi-Fi `192.168.0.19` |
| 펌웨어 | `firmware_xiao_vision` @ `fbc5e5a` · `XIAO_ESP32S3:PSRAM=opi` |
| 쓰기 | 부트로더·파티션·boot_app0·앱 4개 영역 `Hash of data verified` |
| 일시 | 2026-09-25 00:0x~00:2x KST |

---

## 1. 코어 버전이 마이크를 가른다

| | `/audio` 5초 수신 | 카메라 |
| --- | --- | --- |
| `esp32:esp32@3.3.11` | **0 바이트** — `AUDIO_OPEN` 뒤 `i2s_read` 에서 막힘 | 24.7 fps · 3,905 kbps |
| **`esp32:esp32@2.0.12`** | **187,392 바이트 / 5.86초** · 피크 3840 · RMS 244 | **31.3 fps · 5,333 kbps** |

`audioHandler` 는 `AUDIO_OPEN` 을 찍은 뒤 `i2s_read(..., portMAX_DELAY)` 로 프라이밍 3회를
돈다. PDM 수신이 서지 않으면 **영원히 막히고 한 바이트도 나가지 않는다.** 3.3.11 의 증상이
정확히 그것이다.

⚠️ **`MIC_READY` 는 마이크가 있다는 뜻이 아니다.** 그 로그는 `i2s_set_clk` 까지 성공하면
찍힌다. 3.3.11 빌드도 `MIC_READY rate=16000 dma_ms=256` 을 정상으로 찍었다.
**이 로그를 근거로 마이크를 판정하면 안 된다** — `/audio` 로 실제 바이트를 받아야 한다.

## 2. `4.2.2` VGA 스트리밍 재측정

```
STREAM_STATS profile=VGA fps=24.7 limit=25 bytes_avg=19775 skipped=15 kbps=3905   (3.3.11)
6초 수신 188 프레임 = 31.3 fps · 프레임 평균 20.8 KB · 5,333 kbps                 (2.0.12)
```

`4.2.2` 에 적힌 «약 2.5 Mbps 천장 · 복잡한 장면 9.4 fps» 는 이 기체·이 조건에서
**재현되지 않았다.**

⚠️ **조건을 함께 읽어야 한다.**
- **PC 를 유선 랜으로 돌려 2.4GHz 를 카메라가 독점**했다. PC 가 같은 대역에 있으면 공중
  시간을 나눠 쓴다.
- **장면이 단순하다** — 프레임 평균 20.8 KB 다. 문제 사례는 31 KB 였다. 사람·물건이 많은
  실제 순찰 장면에서 다시 봐야 한다.
- ⚠️ **강제 냉각 중에 잰 값이다** — 발열로 보드를 송풍구 위에 올려 둔 상태였다.
  로봇에 장착하면 그 냉각이 없다. `3-1`(2시간 연속)·`6.4.3`(10분 무개입)에 그대로 걸린다.

## 3. 재현

```bash
arduino-cli compile --fqbn "esp32:esp32:XIAO_ESP32S3:PSRAM=opi" firmware_xiao_vision
```

```bash
curl -s --max-time 6 -o audio.raw "http://<xiao>:82/audio?gain=2"
```

`audio.raw` 가 0 바이트면 코어 버전을 먼저 본다. 16 kHz PCM16LE 모노이므로 5초면 약 160 KB 다.

⚠️ Wi-Fi 자격증명은 `firmware_xiao_vision/wifi_secrets.h` 로 넣는다(`.gitignore` 등록됨).
저장소의 `YOUR_WIFI_SSID` 자리표시자 그대로 구우면 **카메라가 네트워크에서 사라진다.**
