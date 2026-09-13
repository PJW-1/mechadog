// ══════════════════════════════════════════════════════════════
//  udp_feed — LD19 바이트를 UART 대신 UDP 로 받아 중계 경로를 시험한다
//
//  목적: LD19→ESP32 물리 배선(듀폰 선) 없이, 파서→조립→인코더→송신
//  체인을 **실물 LD19 바이트**로 검증한다. PC 가 COM10 에서 읽은
//  시리얼 바이트를 UDP 5202 로내면, 이 스케치가 그 바이트를
//  UART 에서 받은 것처럼 Ld19Parser 에 흘려보낸다.
//
//    LD19 ─USB─▶ PC(ld19_serial_relay --fwd-host <esp-ip>)
//                     └─ UDP 5202 ─▶ 이 스케치 ─UDP 5201─▶ PC 수신기
//
//  검증 범위: 파서·조립기·인코더·청크 송신 — 실물 바이트 기준.
//  검증 불가: GPIO16 물리 UART 수신(전기적 경로) — 배선 후 별도 확인.
//
//  컴파일 시 상위 src 를 -I 로 넣는다 (lidar_self_test 와 같은 방식):
//    --build-property "compiler.cpp.extra_flags=-I<repo>/firmware_lidar_relay/src"
// ══════════════════════════════════════════════════════════════

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

#ifndef LIDAR_FEED_PORT
#define LIDAR_FEED_PORT 5202  // PC → 이 스케치 (생 LD19 바이트)
#endif
#ifndef LIDAR_HOST_PORT
#define LIDAR_HOST_PORT 5201  // 이 스케치 → PC (SCAN JSON)
#endif

constexpr size_t kChunkPoints = 72;

static mechadog::ScanPoint scan_points[mechadog::ScanAssembler::kMaxScanPoints];
static char json_buf[mechadog::ScanEncoder::CapacityFor(kChunkPoints + 16)];

mechadog::Ld19Parser parser;
mechadog::ScanAssembler assembler;
mechadog::ScanEncoder encoder;
WiFiUDP feed_udp;   // 수신용
WiFiUDP scan_udp;   // 송신용

char device_id[32];
char boot_id[24];
IPAddress host_ip;
bool send_enabled = false;

uint32_t feed_pkts = 0;
uint32_t feed_bytes = 0;
uint32_t scans_sent = 0;
uint32_t last_stats_ms = 0;

int64_t NowMs() { return esp_timer_get_time() / 1000; }

void makeDeviceId() {
  uint8_t mac[6];
  esp_efuse_mac_get_default(mac);
  snprintf(device_id, sizeof(device_id), "lidar-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
           mac[3], mac[4], mac[5]);
}

void makeBootId() {
  uint64_t r = ((uint64_t)esp_random() << 32) | esp_random();
  snprintf(boot_id, sizeof(boot_id), "%016llx", (unsigned long long)r);
}

void connectWifi() {
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);
  Serial.printf("[udp-feed] WiFi connecting to %s", MECHDOG_WIFI_SSID);
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; ++i) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[udp-feed] connected ip=%s rssi=%d dBm\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
  } else {
    Serial.println("[udp-feed] wifi failed");
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[udp-feed] boot");

  WiFi.mode(WIFI_STA);
  makeDeviceId();
  makeBootId();
  encoder.begin(device_id, boot_id);
  Serial.printf("[udp-feed] device_id=%s boot_id=%s\n", device_id, boot_id);

  send_enabled = host_ip.fromString(LIDAR_HOST_IP) && host_ip != IPAddress(0, 0, 0, 0);
  if (send_enabled) {
    Serial.printf("[udp-feed] target %s:%d\n", LIDAR_HOST_IP, LIDAR_HOST_PORT);
    connectWifi();
  }

  feed_udp.begin(LIDAR_FEED_PORT);
  Serial.printf("[udp-feed] listening udp:%d — PC 가 LD19 생 바이트를 보내라\n", LIDAR_FEED_PORT);
}

void sendScan(const mechadog::ScanPoint* points, size_t n) {
  for (size_t off = 0; off < n; off += kChunkPoints) {
    const size_t take = (n - off < kChunkPoints) ? (n - off) : kChunkPoints;
    const auto res = encoder.encode(points + off, take, NowMs(), json_buf, sizeof(json_buf));
    if (!res.ok) {
      Serial.printf("[udp-feed] encode failed: %s\n", res.reason);
      return;
    }
    if (!send_enabled || WiFi.status() != WL_CONNECTED) return;
    scan_udp.beginPacket(host_ip, LIDAR_HOST_PORT);
    scan_udp.write(reinterpret_cast<const uint8_t*>(json_buf), res.length);
    if (scan_udp.endPacket() == 1) {
      ++scans_sent;
    }
  }
}

void loop() {
  // ── UDP 수신 → 바이트 단위 파서 투입 (UART 경로와 동일 코드) ──
  int pkt = feed_udp.parsePacket();
  while (pkt > 0) {
    ++feed_pkts;
    feed_bytes += pkt;
    while (feed_udp.available() > 0) {
      const int b = feed_udp.read();
      if (b < 0) break;
      if (!parser.feed(static_cast<uint8_t>(b))) continue;

      const size_t n = assembler.addFrame(parser.frame(), scan_points,
                                          sizeof(scan_points) / sizeof(scan_points[0]));
      if (n > 0) sendScan(scan_points, n);
      while (assembler.pending() >= kChunkPoints) {
        const size_t chunk =
            assembler.flush(scan_points, sizeof(scan_points) / sizeof(scan_points[0]));
        if (chunk == 0) break;
        sendScan(scan_points, chunk);
      }
    }
    pkt = feed_udp.parsePacket();
  }

  if (send_enabled && WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }

  if (millis() - last_stats_ms > 5000) {
    last_stats_ms = millis();
    Serial.printf(
        "[udp-feed] pkts=%u bytes=%u frames=%u crc_fail=%u bad_verlen=%u resync=%u "
        "scans=%u sent=%u rssi=%d\n",
        feed_pkts, feed_bytes, parser.frames_ok(), parser.crc_failures(), parser.bad_verlen(),
        parser.resyncs(), assembler.scans_completed(), scans_sent, WiFi.RSSI());
  }
}
