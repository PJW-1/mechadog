// ══════════════════════════════════════════════════════════════
//  LD19 UART 프레임 파서 + 한 바퀴 스캔 조립기
//
//  정본 : docs/PROTOCOL_LIDAR.md 2절 — 이 노드가보내는 데이터그램
//  센서 : LD19 개발 매뉴얼 V2.3 — UART 230400 8N1, 단방향 스트림
//
//  ── 프레임 구조 (47바이트 고정) ─────────────────────────────
//   [0]      0x54          헤더
//   [1]      0x2C          VerLen (상위 3비트 타입=1, 하위 5비트 점 수=12)
//   [2:4]    speed         회전 속도, deg/s, LE
//   [4:6]    start_angle   0.01도 단위, LE
//   [6:42]   12 × (dist_mm u16 LE + intensity u8)
//   [42:44]  end_angle     0.01도 단위, LE
//   [44:46]  timestamp     ms, LE
//   [46]     CRC8          poly 0x4D, init 0x00, 비반사, xorout 0x00
//                          (바이트 [0..45] 에 대해 계산)
//
//  ── HAL 비의존 ──────────────────────────────────────────────
//  이 파일과 ld19.cpp 는 Arduino.h 를 포함하지 않는다. 하드웨어 없이
//  호스트 컴파일로 단위시험이 가능해야 하기 때문이다
//  (CONTRIBUTING 7절 HAL 분리 원칙).
//
//  ── 각도 부호 ───────────────────────────────────────────────
//  LD19 는 각도를 반시계(CCW)로보낸다. 우리 규약의 angle_deg 도
//  로봇 기준 CCW 이므로 여기서 부호를 뒤집지 않는다
//  (PROTOCOL_LIDAR 6절 — CW 제품이면 "중계 노드에서" 뒤집으라고
//  정해 둔 자리가 이 클래스다).
//
//  ⚠️ UART 로는 무엇이든 들어온다. 이 파서는 어떤 바이트열을 받아도
//     크래시하지 않아야 하고, 힙 할당·예외를 쓰지 않는다.
// ══════════════════════════════════════════════════════════════

#ifndef MECHADOG_FIRMWARE_LIDAR_RELAY_LD19_H
#define MECHADOG_FIRMWARE_LIDAR_RELAY_LD19_H

#include <stddef.h>
#include <stdint.h>

namespace mechadog {

// ── 프레임 상수 ─────────────────────────────────────────────
constexpr size_t kLd19FrameBytes = 47;
constexpr uint8_t kLd19Header = 0x54;
constexpr uint8_t kLd19VerLen = 0x2C;  // 타입 1, 12개 측정점
constexpr size_t kLd19PointsPerFrame = 12;
constexpr int32_t kCdegPerRevolution = 36000;  // 0.01도 단위

struct Ld19MeasPoint {
  uint16_t dist_mm = 0;   // 0 = 센서의 측정 실패 값 (PROTOCOL_LIDAR 2절)
  uint8_t intensity = 0;  // wire 의 quality 원소로 그대로 실린다
};

struct Ld19Frame {
  uint16_t speed_dps = 0;   // deg/s
  uint16_t start_cdeg = 0;  // 0.01 deg, 0..35999
  uint16_t end_cdeg = 0;
  uint16_t stamp_ms = 0;  // 센서 내부 ms 카운터 (포장 전송에 쓰지 않음)
  Ld19MeasPoint points[kLd19PointsPerFrame];
};

// ── 바이트 스트림 → 프레임 ──────────────────────────────────
// feed() 가 true 를 돌려주면 frame() 에 완성 프레임이 들어 있다.
// CRC 실패·기형 VerLen 은 버리고 버퍼 안에서 0x54 를 다시 찾아
// 재동기화한다. 카운터는 진단용이다.
class Ld19Parser {
 public:
  // 한 바이트를 먹인다. 완성 프레임이 검증을 통과하면 true.
  bool feed(uint8_t byte);
  const Ld19Frame& frame() const { return frame_; }

  uint32_t frames_ok() const { return frames_ok_; }
  uint32_t crc_failures() const { return crc_failures_; }
  uint32_t bad_verlen() const { return bad_verlen_; }
  uint32_t resyncs() const { return resyncs_; }

 private:
  // 버퍼 안에서 다음 헤더를 찾아 남은 바이트를 앞으로 당긴다.
  void resync_();

  uint8_t buf_[kLd19FrameBytes] = {};
  size_t len_ = 0;
  Ld19Frame frame_ = {};
  uint32_t frames_ok_ = 0;
  uint32_t crc_failures_ = 0;
  uint32_t bad_verlen_ = 0;
  uint32_t resyncs_ = 0;
};

// CRC8 — poly 0x4D, init 0x00, no reflect, xorout 0x00.
// data 는 프레임의 [0..45] 46바이트. 반환값이 [46] 과 같아야 유효다.
uint8_t Ld19Crc8(const uint8_t* data, size_t len);

// ── 스캔 조립 ───────────────────────────────────────────────
// LD19 프레임은 각도 순서대로 도착하며, 한 프레임이 0°/360° 경계를
// 걸칠 수 있다(end < start). 한 회전 안에서는 프레임의 start 각도가
// 단조 증가한다 — 새 프레임의 start 가 직전 것보다 작으면 새 회전이
// 시작된 것이고, 지금까지 모은 점들이 완성된 한 바퀴다.
//
// 점별 각도는 start→end 를 12점에 걸쳐 선형 보간한다. end < start
// 이면 그 프레임이 0° 를 걸친 것이므로 end 에 한 바퀴를 더해 계산한다.

struct ScanPoint {
  uint16_t angle_cdeg = 0;  // 0..35999 (0.01 deg)
  uint16_t dist_mm = 0;
  uint8_t quality = 0;
};

class ScanAssembler {
 public:
  // 한 바퀴의 점 상한. LD19 는 초당 ~4500점이므로 10Hz 회전에서
  // ~450점, 5Hz 까지 느려져도 ~900점이다. 상한을 넘는 점은 버리고
  // points_dropped_ 로 센다 — 버퍼가 무한히 자라는 것을 막는다.
  static constexpr size_t kMaxScanPoints = 1200;

  // 새 프레임을 먹인다. 한 바퀴가 완성되면 모은 점을 out 에 복사하고
  // 그 점 수를 돌려준다. 아직 진행 중이면 0.
  // out 은 kMaxScanPoints 개를 담을 수 있어야 한다 — 더 작으면
  // 넘치는 점은 잘린다.
  size_t addFrame(const Ld19Frame& f, ScanPoint* out, size_t out_capacity);

  // 한 바퀴를 기다리지 않고 지금까지 모은 점을 꺼낸다.
  // ESP32 Arduino 의 WiFiUDP TX 버퍼가 MTU(~1460B)라 한 바퀴 전체
  // (~450점 ≈ 6KB)는 한 데이터그램에 못 들어간다 — 부채꼴 단위로
  // 잘라 보낼 때 쓴다. 호스트는 규칙 ⑥ 과 각도 빈 집계로 부분
  // 스캔도 그대로 받아들인다.
  size_t flush(ScanPoint* out, size_t out_capacity);

  size_t pending() const { return count_; }
  uint32_t scans_completed() const { return scans_completed_; }
  uint32_t points_dropped() const { return points_dropped_; }

 private:
  ScanPoint points_[kMaxScanPoints] = {};
  size_t count_ = 0;
  uint16_t last_start_cdeg_ = 0;
  bool have_last_ = false;
  uint32_t scans_completed_ = 0;
  uint32_t points_dropped_ = 0;
};

}  // namespace mechadog

#endif  // MECHADOG_FIRMWARE_LIDAR_RELAY_LD19_H
