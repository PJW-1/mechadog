// Wi-Fi + UDP 연결 프로브 — LiDAR 중계 노드(COM9)가 이 PC 에
// 데이터그램을 실제로 보낼 수 있는지 확인하는 1회성 진단 스케치다.
//
// 동작:
//   1. wifi_secrets.h 의 SSID/비밀번호로 STA 접속
//   2. 접속되면 IP·RSSI 를 시리얼에 출력
//   3. 1초마다 {"probe":N,...} JSON 을 HOST_IP:5201 로 UDP 송신
//
// PC 측 수신 확인:
//   python -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('0.0.0.0',5201)); print(s.recvfrom(65535))"

#include <WiFi.h>
#include <WiFiUdp.h>

#include "wifi_secrets.h"  // MECHDOG_WIFI_SSID / MECHDOG_WIFI_PASSWORD / LIDAR_HOST_IP

#ifndef LIDAR_HOST_IP
#define LIDAR_HOST_IP "192.168.0.29"
#endif
#ifndef LIDAR_HOST_PORT
#define LIDAR_HOST_PORT 5201
#endif

WiFiUDP udp;
uint32_t probe_seq = 0;
uint32_t last_send_ms = 0;
uint32_t last_retry_ms = 0;

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);
  Serial.printf("[probe] WiFi connecting to %s", MECHDOG_WIFI_SSID);
  const uint32_t deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[probe] connected ip=%s rssi=%d dBm\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI());
    udp.begin(0);  // 임의 로컬 포트 — 송신 전용
  } else {
    Serial.printf("[probe] connect FAILED status=%d\n", WiFi.status());
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[probe] boot");
  connectWifi();
}

void loop() {
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (now - last_retry_ms > 10000) {
      last_retry_ms = now;
      Serial.println("[probe] wifi lost — reconnecting");
      connectWifi();
    }
    return;
  }

  if (now - last_send_ms >= 1000) {
    last_send_ms = now;
    ++probe_seq;
    char payload[96];
    const int n = snprintf(payload, sizeof(payload), "{\"probe\":%u,\"ts\":%u,\"rssi\":%d}",
                           probe_seq, now, WiFi.RSSI());
    udp.beginPacket(LIDAR_HOST_IP, LIDAR_HOST_PORT);
    udp.write(reinterpret_cast<const uint8_t*>(payload), n);
    const bool ok = udp.endPacket() == 1;
    Serial.printf("[probe] seq=%u -> %s:%d %s (%s)\n", probe_seq, LIDAR_HOST_IP, LIDAR_HOST_PORT,
                  ok ? "sent" : "SEND FAILED", payload);
  }
}
