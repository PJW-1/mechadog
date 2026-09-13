// ══════════════════════════════════════════════════════════════
//  firmware_lidar_relay — LD19 → UART2 → ESP32 → Wi-Fi UDP → Host PC
//
//  정본 : docs/PROTOCOL_LIDAR.md · docs/HARDWARE.md(LD19 배선)
//  링크 : LiDAR 중계 노드 → Host PC, UDP 5201 (lidar.scan_port)
//
//  배선 (HARDWARE.md):
//    LD19 Pin4 P5V → DevKit 5V   ·   Pin3 GND → GND
//    LD19 Pin1 Tx  → GPIO16 (UART2 RX)   ·   Pin2 PWM → 미연결
//
//  동작:
//    UART2(230400 8N1, RX=GPIO16) 에서 LD19 프레임을 받아 한 바퀴를
//    모으고, SCAN 데이터그램(JSON)을 만들어 Host PC 로보낸다.
//    제어 명령·텔레메트리 링크와 무관한 독립 노드다 (ADR-6).
//
//  설정: wifi_secrets.example.h 를 wifi_secrets.h 로 복사해 채운다.
// ══════════════════════════════════════════════════════════════

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_system.h>
#include <esp_timer.h>

#include "src/ld19.h"
#include "src/scan_encoder.h"

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
// CI/무설정 빌드용 자리 — 시리얼에 경고만 띄우고 파서는 돌린다.
#define MECHDOG_WIFI_SSID ""
#define MECHDOG_WIFI_PASSWORD ""
#define LIDAR_HOST_IP "0.0.0.0"
#define LIDAR_HOST_PORT 5201
#endif

#ifndef LIDAR_HOST_IP
#define LIDAR_HOST_IP "0.0.0.0"
#endif
#ifndef LIDAR_HOST_PORT
#define LIDAR_HOST_PORT 5201
#endif

// HARDWARE.md 권장 배선 — LD19 Tx 는 GPIO16. PWM 선은 미연결(내부 10Hz).
constexpr int kLidarRxPin = 16;
constexpr int kLidarTxPin = -1;  // LD19 는 단방향 — TX 는 쓰지 않는다
constexpr uint32_t kLidarBaud = 230400;

// 한 데이터그램에 넣는 점 수 상한. WiFiUDP TX 버퍼가 MTU(~1460B)라
// 한 바퀴(~450점 ≈ 6KB)는 통째로 못 간다 — 부채꼴 청크로 자른다.
// 점당 최대 "[359.99,45000],"=15B 이므로 72점 ≈ 1.2KB + 헤더로 충분히
// 들어간다. 스캔 두절은 안전 문제가 아니므로 잘려 보내는 일은 없다.
constexpr size_t kChunkPoints = 72;

static mechadog::ScanPoint scan_points[mechadog::ScanAssembler::kMaxScanPoints];
static char json_buf[mechadog::ScanEncoder::CapacityFor(kChunkPoints + 16)];

mechadog::Ld19Parser parser;
mechadog::ScanAssembler assembler;
mechadog::ScanEncoder encoder;
WiFiUDP udp;

char device_id[32];
char boot_id[24];
IPAddress host_ip;
bool send_enabled = false;

uint32_t last_stats_ms = 0;
uint32_t last_wifi_retry_ms = 0;
uint32_t scans_sent = 0;

int64_t NowMs() {
  return esp_timer_get_time() / 1000;  // uptime ms — 노드 자체 시계
}

void makeDeviceId() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(device_id, sizeof(device_id), "lidar-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
           mac[3], mac[4], mac[5]);
}

void makeBootId() {
  // 부팅마다 새 64비트 난수의 16자리 hex (PROTOCOL_LIDAR 2절 권장)
  snprintf(boot_id, sizeof(boot_id), "%08x%08x", esp_random(), esp_random());
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);
  Serial.printf("[lidar] WiFi connecting to %s", MECHDOG_WIFI_SSID);
  const uint32_t deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[lidar] connected ip=%s rssi=%d dBm\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
    udp.begin(0);  // 송신 전용 — 임의 로컬 포트
  } else {
    Serial.printf("[lidar] connect FAILED status=%d\n", WiFi.status());
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[lidar] boot");

  WiFi.mode(WIFI_STA);  // MAC 읽기 전에 STA 모드 필요
  makeDeviceId();
  makeBootId();
  encoder.begin(device_id, boot_id);
  Serial.printf("[lidar] device_id=%s boot_id=%s\n", device_id, boot_id);

  send_enabled = host_ip.fromString(LIDAR_HOST_IP) && host_ip != IPAddress(0, 0, 0, 0);
  if (send_enabled) {
    Serial.printf("[lidar] target %s:%d\n", LIDAR_HOST_IP, LIDAR_HOST_PORT);
    connectWifi();
  } else {
    Serial.println("[lidar] LIDAR_HOST_IP 미설정 — 파싱만 하고 송신은 안 한다");
  }

  // UART2 — RX 만 쓴다
  Serial2.begin(kLidarBaud, SERIAL_8N1, kLidarRxPin, kLidarTxPin);
  Serial2.setRxBufferSize(2048);  // 47B 프레임이 ms 당 수 개씩 들어온다
  Serial.printf("[lidar] UART2 rx=GPIO%d %u 8N1\n", kLidarRxPin, kLidarBaud);
}

void sendScan(const mechadog::ScanPoint* points, size_t n) {
  const auto res = encoder.encode(points, n, NowMs(), json_buf, sizeof(json_buf));
  if (!res.ok) {
    Serial.printf("[lidar] encode failed: %s\n", res.reason);
    return;
  }
  if (!send_enabled || WiFi.status() != WL_CONNECTED) return;

  udp.beginPacket(host_ip, LIDAR_HOST_PORT);
  udp.write(reinterpret_cast<const uint8_t*>(json_buf), res.length);
  if (udp.endPacket() == 1) {
    ++scans_sent;
  }
}

void loop() {
  // ── UART 드레인 → 프레임 → 스캔 ──
  while (Serial2.available() > 0) {
    const int b = Serial2.read();
    if (b < 0) break;
    if (!parser.feed(static_cast<uint8_t>(b))) continue;

    // 한 바퀴 경계에서 남은 점들을 방출하고, 평시에는 청크 크기마다 자른다.
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

  // ── Wi-Fi 감시 ──
  if (send_enabled && WiFi.status() != WL_CONNECTED && millis() - last_wifi_retry_ms > 10000) {
    last_wifi_retry_ms = millis();
    Serial.println("[lidar] wifi lost — reconnecting");
    connectWifi();
  }

  // ── 5초마다 진단 ──
  if (millis() - last_stats_ms > 5000) {
    last_stats_ms = millis();
    Serial.printf(
        "[lidar] frames=%lu crc_fail=%lu bad_verlen=%lu resync=%lu "
        "scans=%lu sent=%lu drop_full=%lu rssi=%d\n",
        static_cast<unsigned long>(parser.frames_ok()),
        static_cast<unsigned long>(parser.crc_failures()),
        static_cast<unsigned long>(parser.bad_verlen()),
        static_cast<unsigned long>(parser.resyncs()),
        static_cast<unsigned long>(assembler.scans_completed()),
        static_cast<unsigned long>(scans_sent),
        static_cast<unsigned long>(assembler.points_dropped()), send_enabled ? WiFi.RSSI() : 0);
  }
}
