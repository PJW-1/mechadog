#include "telemetry_encoder.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

namespace mechadog {
namespace {

// Reject malformed UTF-8 instead of emitting JSON the host cannot decode.
// The length pass also bounds every continuation-byte read.
bool CopyIdentity(const char* source, char* target) {
  if (source == nullptr) return false;
  size_t length = 0;
  while (length <= TelemetryEncoder::kMaxIdentityBytes && source[length] != '\0') {
    ++length;
  }
  if (length == 0 || length > TelemetryEncoder::kMaxIdentityBytes) return false;
  size_t i = 0;
  while (i < length) {
    const auto lead = static_cast<unsigned char>(source[i++]);
    if (lead < 0x80) continue;
    size_t trailing = 0;
    uint32_t value = 0;
    uint32_t minimum = 0;
    if (lead >= 0xC2 && lead <= 0xDF) {
      trailing = 1;
      value = lead & 0x1F;
      minimum = 0x80;
    } else if (lead >= 0xE0 && lead <= 0xEF) {
      trailing = 2;
      value = lead & 0x0F;
      minimum = 0x800;
    } else if (lead >= 0xF0 && lead <= 0xF4) {
      trailing = 3;
      value = lead & 0x07;
      minimum = 0x10000;
    } else {
      return false;
    }
    if (trailing > length - i) return false;
    for (size_t j = 0; j < trailing; ++j) {
      const auto byte = static_cast<unsigned char>(source[i++]);
      if ((byte & 0xC0) != 0x80) return false;
      value = (value << 6) | (byte & 0x3F);
    }
    if (value < minimum || value > 0x10FFFF || (value >= 0xD800 && value <= 0xDFFF)) {
      return false;
    }
  }
  memcpy(target, source, length + 1);
  return true;
}

class JsonWriter {
 public:
  JsonWriter(char* out, size_t capacity) : out_(out), capacity_(capacity) {
    ok_ = out != nullptr && capacity > 0;
    if (ok_) out_[0] = '\0';
  }

  void character(char value) {
    if (!ok_) return;
    if (size_ >= capacity_ - 1) {
      ok_ = false;
      return;
    }
    out_[size_++] = value;
    out_[size_] = '\0';
  }

  void text(const char* value) {
    while (ok_ && *value != '\0') character(*value++);
  }

  void string(const char* value) {
    static constexpr char kHex[] = "0123456789abcdef";
    character('"');
    while (ok_ && *value != '\0') {
      const auto byte = static_cast<unsigned char>(*value++);
      if (byte == '"' || byte == '\\') {
        character('\\');
        character(static_cast<char>(byte));
      } else if (byte < 0x20) {
        text("\\u00");
        character(kHex[byte >> 4]);
        character(kHex[byte & 0x0F]);
      } else {
        character(static_cast<char>(byte));
      }
    }
    character('"');
  }

  void integer(int64_t value) {
    // All wire integers are checked nonnegative before reaching the writer.
    char digits[20];
    size_t used = 0;
    do {
      digits[used++] = static_cast<char>('0' + value % 10);
      value /= 10;
    } while (value != 0);
    while (used > 0) character(digits[--used]);
  }

  void number(double value) {
    // 17 significant digits preserve double values at range boundaries.
    // ESP32 uses the C locale. If another runtime formats non-JSON decimal
    // punctuation, fail rather than sending malformed JSON or changed values.
    char number[64];
    const int length = snprintf(number, sizeof(number), "%.17g", value);
    if (length < 1 || static_cast<size_t>(length) >= sizeof(number)) {
      ok_ = false;
      return;
    }
    for (int i = 0; i < length; ++i) {
      const char c = number[i];
      if (!((c >= '0' && c <= '9') || c == '.' || c == '-' || c == '+' || c == 'e' || c == 'E')) {
        ok_ = false;
        return;
      }
    }
    text(number);
  }

  void boolean(bool value) { text(value ? "true" : "false"); }
  bool ok() const { return ok_; }
  size_t size() const { return size_; }

 private:
  char* out_;
  size_t capacity_;
  size_t size_ = 0;
  bool ok_ = false;
};

bool ValidInteger(int64_t value, int64_t minimum) {
  return value >= minimum && value <= TelemetryEncoder::kMaxWireInteger;
}

const char* InvalidSampleReason(const TelemetrySample& sample) {
  if (!sample.sensors_valid) return "sensor sample unavailable or stale";
  if (strcmp(to_string(sample.state), "UNKNOWN") == 0) return "unknown state";
  if (!isfinite(sample.dist_cm) || sample.dist_cm < 0.0) return "invalid distance";
  if (!isfinite(sample.pitch) || !isfinite(sample.roll) || !isfinite(sample.yaw)) {
    return "non-finite IMU value";
  }
  if (sample.yaw < 0.0 || sample.yaw >= 360.0) return "yaw outside [0,360)";
  // Upper bound carries a 0.2 V measurement margin over the 8.4 V full charge (ADR-30).
  if (!isfinite(sample.batt_v) || sample.batt_v < 6.0 || sample.batt_v > 8.6) {
    return "battery outside [6.0,8.6]";
  }
  if (!ValidInteger(sample.last_cmd_age_ms, 0)) return "invalid command age";
  if (sample.tipped && sample.state != FsmState::Failsafe) {
    return "tipped requires FAILSAFE";
  }
  return nullptr;
}

}  // namespace

bool TelemetryEncoder::begin(const char* device_id, const char* boot_id, int64_t start_seq) {
  configured_ = false;
  device_id_[0] = '\0';
  boot_id_[0] = '\0';
  if (!ValidInteger(start_seq, 1) || !CopyIdentity(device_id, device_id_) ||
      !CopyIdentity(boot_id, boot_id_)) {
    return false;
  }
  next_seq_ = start_seq;
  configured_ = true;
  return true;
}

TelemetryEncodeResult TelemetryEncoder::encode(const TelemetrySample& sample, int64_t epoch_ms,
                                               char* out, size_t capacity) {
  if (out != nullptr && capacity > 0) out[0] = '\0';
  if (!configured_) return {false, 0, "encoder not configured"};
  if (!ValidInteger(next_seq_, 1))
    return {false, 0, "sequence exhausted; new boot session required"};
  if (!ValidInteger(epoch_ms, 0)) return {false, 0, "invalid epoch timestamp"};
  const char* invalid = InvalidSampleReason(sample);
  if (invalid != nullptr) return {false, 0, invalid};

  JsonWriter writer(out, capacity);
  writer.text("{\"seq\":");
  writer.integer(next_seq_);
  writer.text(",\"ts\":");
  writer.integer(epoch_ms);
  writer.text(",\"device_id\":");
  writer.string(device_id_);
  writer.text(",\"boot_id\":");
  writer.string(boot_id_);
  writer.text(",\"state\":");
  writer.string(to_string(sample.state));
  writer.text(",\"dist_cm\":");
  writer.number(sample.dist_cm);
  writer.text(",\"imu\":{\"pitch\":");
  writer.number(sample.pitch);
  writer.text(",\"roll\":");
  writer.number(sample.roll);
  writer.text(",\"yaw\":");
  writer.number(sample.yaw);
  writer.text("},\"batt_v\":");
  writer.number(sample.batt_v);
  writer.text(",\"last_cmd_age_ms\":");
  writer.integer(sample.last_cmd_age_ms);
  writer.text(",\"flags\":{\"lowbatt\":");
  writer.boolean(sample.lowbatt);
  writer.text(",\"tipped\":");
  writer.boolean(sample.tipped);
  writer.text(",\"link_ok\":");
  writer.boolean(sample.link_ok);
  if (sample.include_obstacle) {
    writer.text(",\"obstacle\":");
    writer.boolean(sample.obstacle);
  }
  writer.text("},\"safety_latched\":");
  writer.boolean(sample.safety_latched);
  writer.character('}');
  if (!writer.ok()) {
    if (out != nullptr && capacity > 0) out[0] = '\0';
    return {false, 0, "output buffer too small or numeric formatting failed"};
  }
  ++next_seq_;
  return {true, writer.size(), ""};
}

}  // namespace mechadog
