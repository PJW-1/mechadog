# TEST_MECHDOG 실기 점검 결과

- 시각: 2026-09-12T02:41:02
- 호스트: 192.168.45.183 (Windows-11-10.0.26200-SP0)
- 훑은 대역: 192.168.45.0/24

## 발견된 노드

| 노드 | 주소 | 비고 |
| :--- | :--- | :--- |
| `mechdog` | `192.168.45.86:5001` | safe_latched=True · actuators=False |
| `xiao` | `192.168.45.245:80` | 센서 OV3660 · RSSI -65dBm |

## 점검 결과

통과 **12** · 실패 **1** · 건너뜀 **1**

| 결과 | 점검 | WBS | 내용 |
| :--- | :--- | :--- | :--- |
| [PASS] | 로봇 UDP 링크 | `2.4.2` | ACK 수신 · verdict=ACCEPT |
| [PASS] | UDP 왕복 시간·손실률 | `2.4.2` | 평균 12.36ms · 최대 47.2ms · 손실 0.0% |
| [PASS] | 기형 패킷 폐기 | `3.1.2` | 5종 전부 폐기 |
| [PASS] | 미지 타입 폐기 (크래시 없음) | `3.1.2` | 폐기 후에도 정상 명령이 통한다 |
| [PASS] | 범위 초과 클램핑 | `3.1.2` | 받아들였다 · verdict=ACCEPT (step 500→100 기대) |
| [PASS] | seq 역전·중복 폐기 | `3.1.2` | 지난 seq(1033) 폐기 |
| [SKIP] | 서보 활성화 여부 | `4.1.2` | 서보 비활성 빌드 (MECHADOG_ENABLE_ACTUATORS=0). 네트워크·파서·안전만 검증된다 — 실패가 아니다 |
| [PASS] | 10Hz 텔레메트리 수신·스키마 | `4.1.4` | 30건 · 10.0Hz · 수락 30건 |
| [PASS] | 명령 타임아웃 페일세이프 | `3.2.1` | 300ms 침묵 후 FAILSAFE 래치 확인 |
| [PASS] | ESTOP 래치 · MOVE 차단 | `3.2.5` | 래치 성립 · 래치 중 MOVE 차단 확인 |
| [PASS] | RESET_SAFE 수동 해제 | `3.2.5` | 래치 해제됨 |
| [PASS] | 카메라 상태 | `4.2.1` | 센서 OV3660 · 프로파일 VGA · RSSI -62dBm |
| [PASS] | 프로파일 전환 VGA@25fps | `4.2.2` | VGA 25fps 로 전환됨 |
| [FAIL] | MJPEG 스트림 수신률 | `6.3.1` | 스트림 수신 실패: Remote end closed connection without response (브라우저가 점유 중인지 확인) |

## 측정값

**로봇 UDP 링크**

```json
{
  "path": "ack",
  "safe_latched": true,
  "actuators": false,
  "failsafe_count": 3
}
```

**UDP 왕복 시간·손실률**

```json
{
  "samples": 30,
  "lost": 0,
  "loss_pct": 0.0,
  "mean_ms": 12.36,
  "p95_ms": 30.26,
  "max_ms": 47.2
}
```

**기형 패킷 폐기**

```json
{
  "cases": [
    "JSON 아님",
    "최상위가 배열",
    "seq 가 실수",
    "type 이 배열",
    "필수 필드 누락"
  ]
}
```

**범위 초과 클램핑**

```json
{
  "verdict": "ACCEPT"
}
```

**서보 활성화 여부**

```json
{
  "actuators": false
}
```

**10Hz 텔레메트리 수신·스키마**

```json
{
  "count": 30,
  "accepted": 30,
  "seconds": 3.0,
  "rate_hz": 10.0,
  "senders": [
    "192.168.45.86"
  ],
  "discard_reasons": []
}
```

**명령 타임아웃 페일세이프**

```json
{
  "timeout_ms": 300,
  "failsafe_count": 4
}
```

**ESTOP 래치 · MOVE 차단**

```json
{
  "failsafe_count": 4
}
```

**카메라 상태**

```json
{
  "ok": true,
  "sensor": "OV3660",
  "profile": "VGA",
  "fps_limit": 25,
  "psram_free": 8247867,
  "rssi": -62,
  "stream": "http://192.168.45.245:81/stream"
}
```

**프로파일 전환 VGA@25fps**

```json
{
  "ok": true,
  "profile": "VGA",
  "fps_limit": 25
}
```

## 건너뛴 항목 — 미구현이며 고장이 아니다

- **서보 활성화 여부** (`4.1.2`) — 서보 비활성 빌드 (MECHADOG_ENABLE_ACTUATORS=0). 네트워크·파서·안전만 검증된다 — 실패가 아니다

