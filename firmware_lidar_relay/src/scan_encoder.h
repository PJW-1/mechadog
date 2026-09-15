// ══════════════════════════════════════════════════════════════
//  LiDAR 스캔 데이터그램 인코더 — 중계 노드 → Host PC (UDP 5201)
//
//  정본 : docs/PROTOCOL_LIDAR.md 2절
//  참조 : host/common/lidar_link.py `encode_scan` — 이 클래스의 참조 구현
//
//  ── 전선 형식 ───────────────────────────────────────────────
//  {"seq":12,"ts":1756800000123,"type":"SCAN","device_id":"lidar-01",
//   "boot_id":"7f3a91c2e8b40d65","points":[[0.0,1250],[1.0,1249]]}
//
//  · seq   : 부팅 안에서 1 부터 단조 증가 — 이것이 곧 Scan ID 다
//  · ts    : 중계 노드 시계 기준 밀리초 (uptime ms — 호스트는 도착
//            시각으로 스톨을 보므로 epoch 정합은 요구하지 않는다)
//  · points: [angle_deg, dist_mm] — LD19 native 단위 그대로.
//            quality 는 규약상 선택이고 호스트가 "현재 쓰지 않는다"
//            고 못박았으므로 싣지 않는다 — 점당 4바이트가 UDP
//            단편화로 돌아온다.
//  · dist_mm = 0 (측정 실패) 도 그대로 싣는다 — 버리는 것은
//    수신측 규칙 ⑥ 의 몫이고, 버린 수가 배선·전원 진단으로 돌아간다.
//
//  ── HAL 비의존 ──────────────────────────────────────────────
//  telemetry_encoder 와 같은 이유로 Arduino.h 없이 호스트 컴파일이
//  가능해야 한다. 힙 할당·예외 없음.
// ══════════════════════════════════════════════════════════════

#ifndef MECHADOG_FIRMWARE_LIDAR_RELAY_SCAN_ENCODER_H
#define MECHADOG_FIRMWARE_LIDAR_RELAY_SCAN_ENCODER_H

#include <stddef.h>
#include <stdint.h>

#include "ld19.h"

namespace mechadog {

struct ScanEncodeResult {
  constexpr ScanEncodeResult() = default;
  constexpr ScanEncodeResult(bool success, size_t bytes, const char* why)
      : ok(success), length(bytes), reason(why) {}
  bool ok = false;
  size_t length = 0;        // NUL 제외. 후행 개행 없음.
  const char* reason = "";  // 정적 문자열, 소유권 없음.
};

class ScanEncoder {
 public:
  // ID 는 각각 64바이트까지 — 호스트 identity 규약의 부분집합이다.
  static constexpr size_t kMaxIdentityBytes = 64;

  // out 버퍼 권장 크기 — 헤더 ~130B + 점당 최대 "[359.99,45000],"=14B.
  static constexpr size_t CapacityFor(size_t points) { return 160 + points * 14; }

  // 호출자가 매 부팅 새 boot_id 를 넣는다. 둘 다 복사한다.
  // 잘못된 설정이면 인코딩을 못 하게 둔다 — 옛 identity 를 유지하지 않는다.
  bool begin(const char* device_id, const char* boot_id, int64_t start_seq = 1);

  // 성공한 인코드만 seq 를 올린다. 실패 시 length=0 이고 capacity>0 이면
  // out[0] 을 지운다. capacity 는 NUL 을 포함한 크기다.
  // UDP 송신 실패는 seq 공백을 남길 수 있고 그것은 규약상 유효하다.
  ScanEncodeResult encode(const ScanPoint* points, size_t count, int64_t ts_ms, char* out,
                          size_t capacity);
  int64_t next_seq() const { return next_seq_; }

 private:
  bool configured_ = false;
  char device_id_[kMaxIdentityBytes + 1] = {};
  char boot_id_[kMaxIdentityBytes + 1] = {};
  int64_t next_seq_ = 1;
};

}  // namespace mechadog

#endif  // MECHADOG_FIRMWARE_LIDAR_RELAY_SCAN_ENCODER_H
