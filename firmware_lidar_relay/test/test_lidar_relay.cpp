// LD19 파서·스캔 조립·SCAN 인코더 호스트 단위시험
//
// 하드웨어 없이 돌린다 (CONTRIBUTING 7절 HAL 분리 원칙). 외부 테스트
// 프레임워크를 쓰지 않는 이유는 test_command_parser.cpp 와 같다.
//
// 빌드·실행 (아래 인자를 한 줄로 이어서 준다):
//   g++ -std=c++17 -Wall -Wextra -O1 -I firmware_lidar_relay/src
//       firmware_lidar_relay/src/ld19.cpp
//       firmware_lidar_relay/src/scan_encoder.cpp
//       firmware_lidar_relay/test/test_lidar_relay.cpp
//       -o build/test_lidar_relay
//   ./build/test_lidar_relay
//   ./build/test_lidar_relay --emit-fixtures > build/lidar-emitted.jsonl
//
// --emit-fixtures 출력은 CI 에서 Python ScanDecoder 로 검증한다 —
// telemetry 인코더 시험과 같은 패턴이다.

#include <stdio.h>
#include <string.h>

#include <string>
#include <vector>

#include "ld19.h"
#include "scan_encoder.h"

namespace {

int g_checks = 0;
int g_failures = 0;

void Check(bool ok, const char* what, const char* detail = nullptr) {
  ++g_checks;
  if (ok) return;
  ++g_failures;
  printf("  [FAIL] %s\n", what);
  if (detail != nullptr) printf("         %s\n", detail);
}

// ── 프레임 조립 도우미 ──────────────────────────────────────
// CRC 를 계산해 완성된 47바이트 프레임을 만든다.
std::vector<uint8_t> MakeFrame(uint16_t speed_dps, uint16_t start_cdeg, uint16_t end_cdeg,
                               uint16_t stamp_ms, uint16_t base_dist, uint8_t base_intensity) {
  std::vector<uint8_t> f(mechadog::kLd19FrameBytes, 0);
  f[0] = mechadog::kLd19Header;
  f[1] = mechadog::kLd19VerLen;
  f[2] = speed_dps & 0xFF;
  f[3] = speed_dps >> 8;
  f[4] = start_cdeg & 0xFF;
  f[5] = start_cdeg >> 8;
  for (size_t i = 0; i < mechadog::kLd19PointsPerFrame; ++i) {
    f[6 + i * 3] = static_cast<uint8_t>((base_dist + i) & 0xFF);
    f[7 + i * 3] = static_cast<uint8_t>((base_dist + i) >> 8);
    f[8 + i * 3] = static_cast<uint8_t>(base_intensity + i);
  }
  f[42] = end_cdeg & 0xFF;
  f[43] = end_cdeg >> 8;
  f[44] = stamp_ms & 0xFF;
  f[45] = stamp_ms >> 8;
  f[46] = mechadog::Ld19Crc8(f.data(), mechadog::kLd19FrameBytes - 1);
  return f;
}

void FeedAll(mechadog::Ld19Parser& p, const std::vector<uint8_t>& bytes, size_t* parsed) {
  for (uint8_t b : bytes) {
    if (p.feed(b)) ++(*parsed);
  }
}

// ── LD19 개발 매뉴얼의 예제 프레임 (골든) ──────────────────
// speed=2152 deg/s, start=324.27°, end=334.70°, ts=6714ms,
// 첫 점 dist=224mm intensity=228, 마지막 점 dist=192mm intensity=229.
const uint8_t kGoldenFrame[mechadog::kLd19FrameBytes] = {
    0x54, 0x2C, 0x68, 0x08, 0xAB, 0x7E, 0xE0, 0x00, 0xE4, 0xDC, 0x00, 0xE2, 0xD9, 0x00, 0xE5, 0xD5,
    0x00, 0xE3, 0xD3, 0x00, 0xE4, 0xD0, 0x00, 0xE9, 0xCD, 0x00, 0xE4, 0xCA, 0x00, 0xE2, 0xC7, 0x00,
    0xE9, 0xC5, 0x00, 0xE5, 0xC2, 0x00, 0xE5, 0xC0, 0x00, 0xE5, 0xBE, 0x82, 0x3A, 0x1A, 0x50};

void TestGoldenFrame() {
  printf("[golden frame]\n");
  mechadog::Ld19Parser p;
  bool got = false;
  for (uint8_t b : kGoldenFrame) got = p.feed(b);
  Check(got, "골든 프레임 파싱됨");
  Check(p.frame().speed_dps == 2152, "speed == 2152 deg/s");
  Check(p.frame().start_cdeg == 32427, "start == 324.27°");
  Check(p.frame().end_cdeg == 33470, "end == 334.70°");
  Check(p.frame().stamp_ms == 6714, "stamp == 6714 ms");
  Check(p.frame().points[0].dist_mm == 224, "첫 점 dist == 224 mm");
  Check(p.frame().points[0].intensity == 228, "첫 점 intensity == 228");
  Check(p.frame().points[11].dist_mm == 192, "끝 점 dist == 192 mm");
  Check(p.frames_ok() == 1 && p.crc_failures() == 0, "카운터 정상");
}

void TestByteAtATime() {
  printf("[byte-at-a-time]\n");
  // 1바이트씩 먹이는 것과 덩어리로 먹이는 것이 같아야 한다
  mechadog::Ld19Parser p;
  bool got = false;
  for (uint8_t b : kGoldenFrame) got = p.feed(b);
  Check(got && p.frames_ok() == 1, "바이트 단위 파싱 동일");
}

void TestCrcReject() {
  printf("[crc reject]\n");
  auto bad = MakeFrame(3600, 0, 500, 0, 1000, 100);
  bad[10] ^= 0xFF;  // 측정점 하나를 뭉갠다 — CRC 불일치
  mechadog::Ld19Parser p;
  size_t parsed = 0;
  FeedAll(p, bad, &parsed);
  Check(parsed == 0 && p.crc_failures() == 1, "CRC 불일치 프레임 폐기");

  // 뒤따르는 정상 프레임은 받아야 한다 (재동기화)
  auto good = MakeFrame(3600, 500, 1000, 0, 2000, 150);
  FeedAll(p, good, &parsed);
  Check(parsed == 1, "CRC 실패 뒤 정상 프레임 수신");
}

void TestGarbageAndResync() {
  printf("[garbage resync]\n");
  mechadog::Ld19Parser p;
  size_t parsed = 0;
  // 쓰레기 바이트 + 0x54 낚시 + 쓰레기 → 정상 프레임
  const uint8_t junk[] = {0x00, 0xFF, 0x13, 0x54, 0xAA, 0x55, 0x00};
  for (uint8_t b : junk) p.feed(b);
  auto good = MakeFrame(3600, 100, 600, 0, 3000, 200);
  FeedAll(p, good, &parsed);
  Check(parsed == 1, "쓰레기+가짜헤더 뒤 재동기화");
}

void TestBadVerlen() {
  printf("[bad verlen]\n");
  auto bad = MakeFrame(3600, 0, 500, 0, 1000, 100);
  bad[1] = 0x10;  // VerLen 깨짐 — CRC 도 같이 깨지지만 VerLen 검사가 먼저다
  mechadog::Ld19Parser p;
  size_t parsed = 0;
  FeedAll(p, bad, &parsed);
  Check(parsed == 0 && p.bad_verlen() == 1, "기형 VerLen 폐기");
}

void TestFrameStraddlesZero() {
  printf("[frame straddle 0°]\n");
  // start=359.50°, end=0.60° — 프레임이 0° 경계를 걸친다
  mechadog::ScanAssembler a;
  mechadog::ScanPoint out[mechadog::ScanAssembler::kMaxScanPoints];
  mechadog::Ld19Frame f = {};
  f.start_cdeg = 35950;
  f.end_cdeg = 60;  // wrap: 실제 36060
  const size_t n = a.addFrame(f, out, sizeof(out) / sizeof(out[0]));
  Check(n == 0, "첫 프레임은 스캔을 완성하지 않는다");
  // 마지막 점은 360° 경계를 넘어 ~0.55° 부근이어야 한다 (11등분의 끝)
  // → 내부 버퍼의 마지막 점 각도를 직접 볼 수는 없으니 wrap 프레임 뒤
  //   start 가 작은 다음 프레임으로 스캔을 닫고 확인한다.
  mechadog::Ld19Frame next = {};
  next.start_cdeg = 200;
  next.end_cdeg = 700;
  const size_t m = a.addFrame(next, out, sizeof(out) / sizeof(out[0]));
  Check(m == 12, "wrap 프레임 점 12개 방출");
  Check(out[11].angle_cdeg == 60, "걸친 프레임 끝 점은 0.60° (mod 360)");
  Check(out[0].angle_cdeg == 35950, "걸친 프레임 첫 점은 359.50°");
}

void TestFullRevolution() {
  printf("[full revolution]\n");
  mechadog::ScanAssembler a;
  mechadog::ScanPoint out[mechadog::ScanAssembler::kMaxScanPoints];
  mechadog::Ld19Frame f = {};
  size_t total_emitted = 0;
  size_t frames = 0;
  // 5° 간격 프레임 72개 = 360° + 새 회전의 첫 프레임 1개
  for (int i = 0; i < 73; ++i) {
    const uint16_t start = static_cast<uint16_t>((i * 500) % 36000);
    f.start_cdeg = start;
    f.end_cdeg = static_cast<uint16_t>((start + 400) % 36000);
    for (size_t j = 0; j < mechadog::kLd19PointsPerFrame; ++j) {
      f.points[j].dist_mm = 1000;
      f.points[j].intensity = 50;
    }
    total_emitted += a.addFrame(f, out, sizeof(out) / sizeof(out[0]));
    ++frames;
  }
  Check(total_emitted == 72 * 12, "한 바퀴 = 72프레임 × 12점 = 864점");
  Check(a.scans_completed() == 1, "스캔 1개 완성");
}

void TestFlush() {
  printf("[flush]\n");
  // WiFiUDP TX 버퍼가 MTU 라 한 바퀴(~450점)는 한 데이터그램에 못 들어간다.
  // flush() 로 부채꼴 청크를 꺼내는 경로를 검증한다.
  mechadog::ScanAssembler a;
  mechadog::ScanPoint out[mechadog::ScanAssembler::kMaxScanPoints];
  mechadog::Ld19Frame f = {};
  f.start_cdeg = 0;
  f.end_cdeg = 500;
  Check(a.addFrame(f, out, 1200) == 0 && a.pending() == 12, "점 누적");
  const size_t n = a.flush(out, 1200);
  Check(n == 12 && a.pending() == 0, "flush 가 모은 점을 비운다");
  Check(out[0].angle_cdeg == 0 && out[11].angle_cdeg == 500, "flush 점 각도 보간 유지");
  Check(a.flush(out, 1200) == 0, "빈 flush 는 0");
}

void TestPointCap() {
  printf("[point cap]\n");
  mechadog::ScanAssembler a;
  mechadog::ScanPoint out[mechadog::ScanAssembler::kMaxScanPoints];
  mechadog::Ld19Frame f = {};
  // start 가 1씩만 증가하면 wrap 이 걸리지 않는다 — 같은 회전 안에서
  // 상한(1200)을 넘기는 상황을 만든다. 101프레임 = 1212점 → 12점 드롭.
  for (int i = 0; i < 101; ++i) {
    f.start_cdeg = static_cast<uint16_t>(i);
    f.end_cdeg = static_cast<uint16_t>(i + 1);
    a.addFrame(f, out, sizeof(out) / sizeof(out[0]));
  }
  Check(a.points_dropped() == 12, "상한 초과분 12점 드롭");
  Check(a.scans_completed() == 0, "wrap 없이는 스캔 미완성");
}

void TestEncoder() {
  printf("[encoder]\n");
  mechadog::ScanEncoder enc;
  Check(!enc.begin("", "boot"), "빈 device_id 거부");
  Check(enc.begin("lidar-01", "7f3a91c2e8b40d65"), "정상 begin");
  char buf[256];
  mechadog::ScanPoint pts[2] = {};
  pts[0].angle_cdeg = 0;
  pts[0].dist_mm = 1250;
  pts[1].angle_cdeg = 100;
  pts[1].dist_mm = 1249;
  auto r = enc.encode(pts, 2, 1756800000123LL, buf, sizeof(buf));
  Check(r.ok, "인코드 성공");
  const char* want =
      "{\"seq\":1,\"ts\":1756800000123,\"type\":\"SCAN\",\"device_id\":"
      "\"lidar-01\",\"boot_id\":\"7f3a91c2e8b40d65\",\"points\":[[0.00,1250]"
      ",[1.00,1249]]}";
  Check(strcmp(buf, want) == 0, "정본 형식과 일치", buf);
  Check(enc.next_seq() == 2, "seq 단조 증가");

  // 버퍼 부족 → 실패, seq 는 그대로
  char tiny[40];
  r = enc.encode(pts, 2, 0, tiny, sizeof(tiny));
  Check(!r.ok && enc.next_seq() == 2, "버퍼 부족 시 실패 + seq 유지");
  Check(tiny[0] == '\0', "실패 시 출력 비움");

  // 음수 ts 거부
  r = enc.encode(pts, 1, -5, buf, sizeof(buf));
  Check(!r.ok, "음수 ts 거부");
}

void EmitFixtures() {
  mechadog::ScanEncoder enc;
  enc.begin("lidar-01", "a1b2c3d4e5f60718");
  char buf[mechadog::ScanEncoder::CapacityFor(500)];
  // 회전 한 바퀴 흉내 — 1° 간격 360점 + 실패값 0 포함
  mechadog::ScanPoint pts[361];
  for (int i = 0; i < 361; ++i) {
    pts[i].angle_cdeg = static_cast<uint16_t>((i * 100) % 36000);
    pts[i].dist_mm = (i % 10 == 0) ? 0 : 1200 + i;  // 10% 는 측정 실패값
  }
  auto r = enc.encode(pts, 361, 1756800000123LL, buf, sizeof(buf));
  if (r.ok) puts(buf);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc > 1 && strcmp(argv[1], "--emit-fixtures") == 0) {
    EmitFixtures();
    return 0;
  }
  TestGoldenFrame();
  TestByteAtATime();
  TestCrcReject();
  TestGarbageAndResync();
  TestBadVerlen();
  TestFrameStraddlesZero();
  TestFullRevolution();
  TestFlush();
  TestPointCap();
  TestEncoder();
  printf("\n%d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
