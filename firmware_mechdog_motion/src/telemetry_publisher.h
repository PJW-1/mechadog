#ifndef MECHADOG_TELEMETRY_PUBLISHER_H
#define MECHADOG_TELEMETRY_PUBLISHER_H

#include <IPAddress.h>
#include <WiFiUdp.h>

#include "telemetry_encoder.h"

namespace mechadog {

// Publication is separate from command ACKs. Sensor acquisition must supply
// a coherent, fresh sample; this class never substitutes missing measurements.
class TelemetryPublisher {
 public:
  bool begin();
  void observe_command(IPAddress host, int64_t host_epoch_ms);
  void poll(const TelemetrySample& sample);
  void disconnected();

 private:
  static constexpr uint16_t kTelemetryPort = 5101;
  static constexpr uint32_t kPeriodMs = 100;
  TelemetryEncoder encoder_;
  WiFiUDP udp_;
  IPAddress host_;
  bool configured_ = false;
  bool have_host_ = false;
  int64_t epoch_at_command_ms_ = 0;
  uint64_t command_uptime_ms_ = 0;
  uint64_t next_publish_ms_ = 0;
  uint64_t next_error_log_ms_ = 0;
  char packet_[1536] = {};
};

}  // namespace mechadog

#endif
