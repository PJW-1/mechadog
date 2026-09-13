// LiDAR 중계 로직 온보드 self-test — 실 타겟에서 파서·조립기·인코더를 검증한다.
//
// 호스트 컴파일러가 없는 환경을 대신해, 같은 골든 벡터를 실기에서 돌린다.
// 마지막에는 인코더가 만든 진짜 SCAN 데이터그램을 UDP 로보내, PC 의
// ScanDecoder(규약 구현)가 수락하는지까지 본다 — 종단간 검증.
//
// PC 측:
//   python tools/lidar_listen.py          (또는 ScanDecoder 로 직접 검증)

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "ld19.h"
#include "scan_encoder.h"

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#define MECHDOG_WIFI_SSID ""
#define MECHDOG_WIFI_PASSWORD ""
#define LIDAR_HOST_IP "0.0.0.0"
#define LIDAR_HOST_PORT 5201
#endif

int g_checks = 0;
int g_failures = 0;

void Check(bool ok, const char* what) {
  ++g_checks;
  Serial.printf("  [%s] %s\n", ok ? "PASS" : "FAIL", what);
  if (!ok) ++g_failures;
}

// CRC 를 계산해 완성 프레임을 만든다
void MakeFrame(uint8_t* f, uint16_t start_cdeg, uint16_t end_cdeg, uint16_t base_dist) {
  memset(f, 0, mechadog::kLd19FrameBytes);
  f[0] = mechadog::kLd19Header;
  f[1] = mechadog::kLd19VerLen;
  f[2] = 0x10;
  f[3] = 0x0E;  // speed 3600 deg/s
  f[4] = start_cdeg & 0xFF;
  f[5] = start_cdeg >> 8;
  for (size_t i = 0; i < mechadog::kLd19PointsPerFrame; ++i) {
    const uint16_t d = base_dist + i;
    f[6 + i * 3] = d & 0xFF;
    f[7 + i * 3] = d >> 8;
    f[8 + i * 3] = 100 + i;
  }
  f[42] = end_cdeg & 0xFF;
  f[43] = end_cdeg >> 8;
  f[46] = mechadog::Ld19Crc8(f, mechadog::kLd19FrameBytes - 1);
}

// LD19 개발 매뉴얼의 예제 프레임 (골든)
const uint8_t kGolden[mechadog::kLd19FrameBytes] = {
    0x54, 0x2C, 0x68, 0x08, 0xAB, 0x7E, 0xE0, 0x00, 0xE4, 0xDC, 0x00, 0xE2, 0xD9, 0x00, 0xE5, 0xD5,
    0x00, 0xE3, 0xD3, 0x00, 0xE4, 0xD0, 0x00, 0xE9, 0xCD, 0x00, 0xE4, 0xCA, 0x00, 0xE2, 0xC7, 0x00,
    0xE9, 0xC5, 0x00, 0xE5, 0xC2, 0x00, 0xE5, 0xC0, 0x00, 0xE5, 0xBE, 0x82, 0x3A, 0x1A, 0x50};

void runTests() {
  mechadog::Ld19Parser p;
  bool got = false;
  for (size_t i = 0; i < sizeof(kGolden); ++i) got = p.feed(kGolden[i]);
  Check(got, "golden frame parsed");
  Check(p.frame().speed_dps == 2152, "speed == 2152 deg/s");
  Check(p.frame().start_cdeg == 32427, "start == 324.27 deg");
  Check(p.frame().end_cdeg == 33470, "end == 334.70 deg");
  Check(p.frame().stamp_ms == 6714, "stamp == 6714 ms");
  Check(p.frame().points[0].dist_mm == 224, "pt0 dist == 224");
  Check(p.frame().points[0].intensity == 228, "pt0 intensity == 228");
  Check(p.frame().points[11].dist_mm == 192, "pt11 dist == 192");

  // CRC 불일치 → 폐기 + 뒤 프레임 재동기화
  uint8_t bad[mechadog::kLd19FrameBytes], good[mechadog::kLd19FrameBytes];
  MakeFrame(bad, 0, 500, 1000);
  bad[10] ^= 0xFF;
  mechadog::Ld19Parser p2;
  bool got2 = false;
  for (size_t i = 0; i < sizeof(bad); ++i) p2.feed(bad[i]);
  Check(p2.frames_ok() == 0 && p2.crc_failures() == 1, "crc fail rejected");
  MakeFrame(good, 500, 1000, 2000);
  for (size_t i = 0; i < sizeof(good); ++i)
    if (p2.feed(good[i])) got2 = true;
  Check(got2, "resync after crc fail");

  // 쓰레기 + 가짜 헤더 뒤 재동기화
  mechadog::Ld19Parser p3;
  const uint8_t junk[] = {0x00, 0xFF, 0x54, 0xAA, 0x55};
  for (uint8_t b : junk) p3.feed(b);
  bool got3 = false;
  for (size_t i = 0; i < sizeof(good); ++i)
    if (p3.feed(good[i])) got3 = true;
  Check(got3, "resync after junk");

  // 0° 를 걸치는 프레임 + 한 바퀴 조립
  static mechadog::ScanAssembler a;
  static mechadog::ScanPoint out[mechadog::ScanAssembler::kMaxScanPoints];
  mechadog::Ld19Frame f = {};
  f.start_cdeg = 35950;
  f.end_cdeg = 60;  // 0° 걸침
  Check(a.addFrame(f, out, 1200) == 0, "first frame no emit");
  f.start_cdeg = 200;
  f.end_cdeg = 700;
  const size_t n = a.addFrame(f, out, 1200);
  Check(n == 12, "wrap frame emits 12 points");
  Check(out[0].angle_cdeg == 35950, "straddle first pt 359.50");
  Check(out[11].angle_cdeg == 60, "straddle last pt 0.60 (mod360)");

  // 한 바퀴: 5° 간격 72프레임 + 새 회전 첫 프레임
  static mechadog::ScanAssembler a2;
  size_t emitted = 0;
  for (int i = 0; i < 73; ++i) {
    mechadog::Ld19Frame fr = {};
    fr.start_cdeg = (i * 500) % 36000;
    fr.end_cdeg = (fr.start_cdeg + 400) % 36000;
    for (size_t j = 0; j < 12; ++j) fr.points[j].dist_mm = 1000;
    emitted += a2.addFrame(fr, out, 1200);
  }
  Check(emitted == 864, "one revolution = 864 points");
  Check(a2.scans_completed() == 1, "scan completed once");

  // 인코더 — 정본 형식 그대로인가
  mechadog::ScanEncoder enc;
  Check(!enc.begin("", "boot"), "empty device_id rejected");
  Check(enc.begin("lidar-01", "7f3a91c2e8b40d65"), "begin ok");
  char buf[256];
  mechadog::ScanPoint pts[2] = {};
  pts[0].dist_mm = 1250;
  pts[1].angle_cdeg = 100;
  pts[1].dist_mm = 1249;
  auto r = enc.encode(pts, 2, 1756800000123LL, buf, sizeof(buf));
  Check(r.ok, "encode ok");
  const char* want =
      "{\"seq\":1,\"ts\":1756800000123,\"type\":\"SCAN\",\"device_id\":"
      "\"lidar-01\",\"boot_id\":\"7f3a91c2e8b40d65\",\"points\":[[0.00,1250]"
      ",[1.00,1249]]}";
  Check(strcmp(buf, want) == 0, "wire format matches spec");
  char tiny[40];
  r = enc.encode(pts, 2, 0, tiny, sizeof(tiny));
  Check(!r.ok && enc.next_seq() == 2, "small buffer fails, seq kept");

  Serial.printf("\n== %d checks, %d failures ==\n", g_checks, g_failures);
}

// 종단간: 인코더가 만든 진짜 SCAN 데이터그램을 실제 링크로보낸다
void sendRealScans() {
  if (strlen(MECHDOG_WIFI_SSID) == 0) {
    Serial.println("[e2e] wifi_secrets.h 없음 — 송신 생략");
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);
  Serial.printf("[e2e] connecting to %s", MECHDOG_WIFI_SSID);
  const uint32_t deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[e2e] wifi FAILED — 송신 생략");
    return;
  }
  Serial.printf("[e2e] connected ip=%s\n", WiFi.localIP().toString().c_str());

  uint8_t mac[6];
  WiFi.macAddress(mac);
  char dev[32], boot[24];
  snprintf(dev, sizeof(dev), "lidar-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3],
           mac[4], mac[5]);
  snprintf(boot, sizeof(boot), "%08x%08x", esp_random(), esp_random());

  mechadog::ScanEncoder enc;
  enc.begin(dev, boot);
  WiFiUDP udp;
  udp.begin(0);
  IPAddress host;
  host.fromString(LIDAR_HOST_IP);

  // 합성 부채꼴: 0.5° 간격 72점 + 10% 측정실패(0) — 수신측 규칙 ⑥ 검증용.
  // WiFiUDP TX 버퍼가 MTU 라 한 데이터그램은 ~72점까지다.
  static mechadog::ScanPoint pts[72];
  for (int i = 0; i < 72; ++i) {
    pts[i].angle_cdeg = i * 50;
    pts[i].dist_mm = (i % 10 == 0) ? 0 : 1200 + i;
  }
  static char json[mechadog::ScanEncoder::CapacityFor(72)];
  for (int s = 0; s < 5; ++s) {
    auto r = enc.encode(pts, 72, esp_timer_get_time() / 1000, json, sizeof(json));
    if (!r.ok) {
      Serial.printf("[e2e] encode failed %s\n", r.reason);
      break;
    }
    udp.beginPacket(host, LIDAR_HOST_PORT);
    udp.write(reinterpret_cast<const uint8_t*>(json), r.length);
    const bool ok = udp.endPacket() == 1;
    Serial.printf("[e2e] seq=%lld %u bytes -> %s:%d %s\n", enc.next_seq() - 1, r.length,
                  LIDAR_HOST_IP, LIDAR_HOST_PORT, ok ? "sent" : "FAILED");
    delay(200);  // 5Hz
  }
  Serial.printf("[e2e] done — device_id=%s boot_id=%s\n", dev, boot);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("[selftest] boot");
  runTests();
  sendRealScans();
  Serial.println("[selftest] done");
}

void loop() {}
