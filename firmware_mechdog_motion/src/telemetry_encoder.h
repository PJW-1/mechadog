// WBS 4.1.4: HAL-independent telemetry serialization.
// Contract: docs/PROTOCOL.md section 5, host/common/protocol.py.
// No sensor acquisition, clocks, network I/O, owned dynamic storage, or exceptions.
// Numeric formatting uses platform snprintf; its libc allocation policy applies.
#ifndef MECHADOG_TELEMETRY_ENCODER_H
#define MECHADOG_TELEMETRY_ENCODER_H

#include <stddef.h>
#include <stdint.h>

#include "command_parser.h"

namespace mechadog {

// All measurements must come from the caller's real sensor acquisition.
// Default values are NOT measurements: encode() rejects sensors_valid=false.
// The caller must also clear sensors_valid on unavailable/stale sensor data.
struct TelemetrySample {
  bool sensors_valid = false;
  FsmState state = FsmState::Unknown;
  double dist_cm = 0.0;
  double pitch = 0.0;
  double roll = 0.0;
  double yaw = 0.0;
  double batt_v = 0.0;
  int64_t last_cmd_age_ms = 0;
  bool lowbatt = false;
  bool tipped = false;
  bool link_ok = false;
  bool obstacle = false;
  bool safety_latched = false;
  // PROTOCOL section 5 permits omission when onboard obstacle-stop reporting
  // is unavailable. Existing callers include the field unless they opt out.
  bool include_obstacle = true;
};

struct TelemetryEncodeResult {
  // ESP32 Arduino core 2.x builds sketches as C++11.
  constexpr TelemetryEncodeResult() = default;
  constexpr TelemetryEncodeResult(bool success, size_t bytes, const char* why)
      : ok(success), length(bytes), reason(why) {}
  bool ok = false;
  size_t length = 0;        // Excludes the terminating NUL. No trailing newline.
  const char* reason = "";  // Static string, no ownership.
};

class TelemetryEncoder {
 public:
  // Fixed storage bounds the embedded encoder. ASCII IDs are recommended;
  // valid UTF-8 is accepted, with each ID limited to 64 BYTES, not characters.
  // This is a bounded subset of the host's identity string contract.
  static constexpr size_t kMaxIdentityBytes = 64;
  static constexpr int64_t kMaxWireInteger = INT64_C(9007199254740991);

  // Caller supplies a new boot_id for each physical boot. Copies both IDs.
  // Invalid configuration disables encoding; it never keeps an old identity.
  bool begin(const char* device_id, const char* boot_id, int64_t start_seq = 1);

  // epoch_ms is supplied by the caller; uptime is not an epoch timestamp.
  // Only a successful encode advances seq. Failed encoding returns length=0
  // and clears out[0] if out is non-null and capacity>0. Capacity includes NUL.
  // A subsequent UDP send failure may leave a sequence gap, which is valid.
  TelemetryEncodeResult encode(const TelemetrySample& sample, int64_t epoch_ms, char* out,
                               size_t capacity);
  int64_t next_seq() const { return next_seq_; }

 private:
  bool configured_ = false;
  char device_id_[kMaxIdentityBytes + 1] = {};
  char boot_id_[kMaxIdentityBytes + 1] = {};
  int64_t next_seq_ = 1;
};

}  // namespace mechadog

#endif  // MECHADOG_TELEMETRY_ENCODER_H
