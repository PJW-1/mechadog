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
  if (!configured_ || !have_host_ || WiFi.status() != WL_CONNECTED || now < next_publish_ms_) {
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
