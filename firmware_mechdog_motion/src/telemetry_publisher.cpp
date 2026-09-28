#include "telemetry_publisher.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>
#include <esp_timer.h>

namespace mechadog {
namespace {

uint64_t uptime_ms() {
  return static_cast<uint64_t>(esp_timer_get_time()) / 1000;
}

}  // namespace

bool TelemetryPublisher::begin() {
  uint8_t mac[6] = {};
  if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) return false;
  char device_id[32];
  snprintf(device_id, sizeof(device_id), "mechdog-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
           mac[3], mac[4], mac[5]);
  char boot_id[17];
  snprintf(boot_id, sizeof(boot_id), "%08lx%08lx", static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()));
  configured_ = encoder_.begin(device_id, boot_id);
  next_publish_ms_ = uptime_ms() + kPeriodMs;
  Serial.printf("Telemetry identity: %s boot=%s port=%u\n", device_id, boot_id, kTelemetryPort);
  return configured_;
}

void TelemetryPublisher::observe_command(IPAddress host, int64_t host_epoch_ms) {
  // PROTOCOL uses Host PC epoch milliseconds. Anchor it to monotonic uptime
  // when an already-validated command arrives; no blocking NTP dependency.
  if (host_epoch_ms < 0 || host_epoch_ms > TelemetryEncoder::kMaxWireInteger) return;
  host_ = host;
  epoch_at_command_ms_ = host_epoch_ms;
  command_uptime_ms_ = uptime_ms();
  have_host_ = true;
}

void TelemetryPublisher::disconnected() {
  udp_.stop();
  have_host_ = false;
}

void TelemetryPublisher::poll(const TelemetrySample& sample) {
  const uint64_t now = uptime_ms();
  // 주기가 아직 안 됐다. 정상이고 초당 아홉 번 지나가므로 기록하지 않는다.
  if (now < next_publish_ms_) {
    return;
  }
  // ⚠️ **나머지 세 관문은 조용히 빠져나가면 안 된다.** 명령 ACK 는 이 검사를 거치지
  // 않고 수신 즉시 응답하므로, 여기서 막히면 **텔레메트리만 멈추고 로그는 한 줄도
  // 남지 않는다.** 2026-09-18 실기에서 그 상태를 만났다 — «ACK 10/s · 텔레메트리
  // 0~2/s · 로그 0건 · PING 20/20 · RTT 3.5ms» 였고, 센서는 UART 로 전부 정상
  // (`imu_valid=1 dist_valid=1 batt_valid=1`)임을 확인했다. 즉 인코더도 송신도
  // 아니었고 남은 곳이 여기뿐인데, **관측 수단이 없어서 그 이상 좁힐 수 없었다.**
  const char* blocked = !configured_                    ? "not configured"
                        : !have_host_                   ? "host unknown"
                        : WiFi.status() != WL_CONNECTED ? "wifi not connected"
                                                        : nullptr;
  if (blocked != nullptr) {
    ++skipped_;
    if (now >= next_error_log_ms_) {
      Serial.printf("Telemetry skipped: %s (total %lu)\n", blocked,
                    static_cast<unsigned long>(skipped_));
      next_error_log_ms_ = now + 1000;
    }
    return;
  }
  // Overruns remain observable as missing receive intervals. Do not burst old
  // samples to make a stalled producer appear to sustain 10Hz.
  next_publish_ms_ += ((now - next_publish_ms_) / kPeriodMs + 1) * kPeriodMs;
  const uint64_t elapsed = now - command_uptime_ms_;
  if (elapsed > static_cast<uint64_t>(TelemetryEncoder::kMaxWireInteger - epoch_at_command_ms_)) {
    return;
  }
  const auto result = encoder_.encode(sample, epoch_at_command_ms_ + static_cast<int64_t>(elapsed),
                                      packet_, sizeof(packet_));
  if (!result.ok) {
    if (now >= next_error_log_ms_) {
      Serial.printf("Telemetry unavailable: %s\n", result.reason);
      next_error_log_ms_ = now + 1000;
    }
    return;
  }
  bool sent = false;
  if (udp_.beginPacket(host_, kTelemetryPort)) {
    const size_t written = udp_.write(reinterpret_cast<const uint8_t*>(packet_), result.length);
    const bool finished = udp_.endPacket() != 0;
    sent = written == result.length && finished;
  }
  if (!sent && now >= next_error_log_ms_) {
    Serial.println("Telemetry UDP send failed");
    next_error_log_ms_ = now + 1000;
  }
}

}  // namespace mechadog
