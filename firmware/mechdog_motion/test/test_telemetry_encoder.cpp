// WBS 4.1.4 host-only tests; this never accesses a robot or network.
// Build with command_parser.cpp and telemetry_encoder.cpp, C++11 or newer.
// --emit-fixtures prints JSONL for independent Python TelemetryDecoder checks.
// --emit-optional-fixtures covers false, omitted, and true obstacle reports.
#include <float.h>
#include <locale.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "telemetry_encoder.h"

namespace {

using mechadog::FsmState;
using mechadog::TelemetryEncoder;
using mechadog::TelemetrySample;

constexpr FsmState kStates[] = {
    FsmState::Patrol,      FsmState::Avoid,  FsmState::Failsafe,       FsmState::Idle,
    FsmState::Scan,        FsmState::Alert,  FsmState::Track,          FsmState::Lost,
    FsmState::AuthWait,    FsmState::Manual, FsmState::HazardDispatch, FsmState::HazardScan,
    FsmState::ZoneInspect,
};
constexpr int64_t kEpoch = INT64_C(1756800000500);
constexpr size_t kBufferSize = 4096;
int g_checks = 0;
int g_failures = 0;

void Check(bool ok, const char* description) {
  ++g_checks;
  if (ok) return;
  ++g_failures;
  printf("[FAIL] %s\n", description);
}

// Explicit synthetic unit-test input, never installed as a sensor provider.
TelemetrySample Sample() {
  TelemetrySample sample;
  sample.sensors_valid = true;
  sample.state = FsmState::Patrol;
  sample.dist_cm = 47.0;
  sample.pitch = 1.25;
  sample.roll = -0.5;
  sample.yaw = 183.5;
  sample.batt_v = 7.625;
  sample.last_cmd_age_ms = 34;
  sample.link_ok = true;
  return sample;
}

bool Emit(TelemetryEncoder& encoder, const TelemetrySample& sample, int64_t ts = kEpoch) {
  char out[kBufferSize];
  const auto result = encoder.encode(sample, ts, out, sizeof(out));
  if (!result.ok) {
    fprintf(stderr, "Fixture encode failed: %s\n", result.reason);
    return false;
  }
  puts(out);
  return true;
}

int EmitFixtures() {
  TelemetryEncoder encoder;
  if (!encoder.begin("mechdog-test", "boot-a")) return 1;
  TelemetrySample sample = Sample();
  for (FsmState state : kStates) {
    sample.state = state;
    sample.tipped = state == FsmState::Failsafe;
    sample.safety_latched = sample.tipped;
    sample.obstacle = state == FsmState::Avoid;
    if (!Emit(encoder, sample)) return 1;
  }
  sample = Sample();
  sample.batt_v = 6.0;
  sample.lowbatt = true;
  sample.dist_cm = 0.0;
  sample.yaw = 0.0;
  sample.last_cmd_age_ms = 0;
  if (!Emit(encoder, sample, 0)) return 1;
  sample.batt_v = 8.6;
  sample.lowbatt = false;
  sample.yaw = nextafter(360.0, 0.0);
  sample.last_cmd_age_ms = TelemetryEncoder::kMaxWireInteger;
  if (!Emit(encoder, sample, TelemetryEncoder::kMaxWireInteger)) return 1;
  sample = Sample();
  sample.dist_cm = DBL_MAX;
  sample.pitch = DBL_MAX;
  sample.roll = -DBL_MAX;
  if (!Emit(encoder, sample)) return 1;
  // The same device's reboot resets seq=1 with a new boot_id.
  if (!encoder.begin("mechdog-test", "boot-b") || !Emit(encoder, Sample())) return 1;
  if (!encoder.begin("dog\"\\\n\t\x01\xea\xb0\x80", "boot\"\\\x1f") || !Emit(encoder, Sample())) {
    return 1;
  }
  return 0;
}

int EmitOptionalFixtures() {
  TelemetryEncoder encoder;
  if (!encoder.begin("mechdog-test", "boot-optional")) return 1;
  TelemetrySample sample = Sample();
  if (!Emit(encoder, sample)) return 1;  // Default inclusion, obstacle=false.
  sample.obstacle = true;
  sample.include_obstacle = false;
  if (!Emit(encoder, sample)) return 1;  // Omit even if a stale value remains.
  sample.include_obstacle = true;
  if (!Emit(encoder, sample)) return 1;  // Explicit inclusion, obstacle=true.
  return 0;
}

void ExpectRejected(TelemetryEncoder& encoder, const TelemetrySample& sample,
                    const char* description, int64_t ts = kEpoch) {
  char out[kBufferSize] = "previous packet";
  const int64_t before = encoder.next_seq();
  const auto result = encoder.encode(sample, ts, out, sizeof(out));
  Check(!result.ok, description);
  Check(result.length == 0 && out[0] == '\0', "failure exposes no partial packet");
  Check(encoder.next_seq() == before, "rejected sample does not consume a sequence");
  Check(result.reason != nullptr && result.reason[0] != '\0', "failure has a reason");
}

void TestConfiguration() {
  TelemetryEncoder encoder;
  ExpectRejected(encoder, Sample(), "unconfigured encoder refuses transmission");
  Check(!encoder.begin(nullptr, "boot"), "null device identity rejected");
  Check(!encoder.begin("dog", nullptr), "null boot identity rejected");
  Check(!encoder.begin("", "boot"), "empty device identity rejected");
  Check(!encoder.begin("dog", ""), "empty boot identity rejected");
  char identity[66];
  memset(identity, 'x', sizeof(identity));
  identity[64] = '\0';
  Check(encoder.begin(identity, identity), "64-byte identities accepted");
  identity[64] = 'x';
  identity[65] = '\0';
  Check(!encoder.begin(identity, "boot"), "overlong device identity rejected");
  Check(!encoder.begin("dog", identity), "overlong boot identity rejected");
  Check(!encoder.begin("dog", "boot", 0), "sequence zero rejected");
  Check(!encoder.begin("dog", "boot", -1), "negative initial sequence rejected");
  Check(!encoder.begin("dog", "boot", TelemetryEncoder::kMaxWireInteger + 1),
        "inexact initial sequence rejected");
  const char* malformed[] = {"\xc0\xaf", "\xed\xa0\x80", "\xf4\x90\x80\x80",
                             "\x80",     "\xe2\x82",     "\xe2\x28\xa1"};
  for (const char* value : malformed) {
    Check(!encoder.begin(value, "boot"), "malformed UTF-8 rejected");
  }
  Check(encoder.begin("dog", "boot"), "valid configuration accepted");
  Check(!encoder.begin("", "boot"), "invalid reconfiguration rejected");
  ExpectRejected(encoder, Sample(), "invalid reconfiguration disables previous identity");

  char device[] = "original";
  char boot[] = "boot-a";
  Check(encoder.begin(device, boot), "identity copy configured");
  device[0] = 'X';
  boot[0] = 'X';
  char out[kBufferSize];
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok, "copied identity encoded");
  Check(strstr(out, "\"device_id\":\"original\"") != nullptr &&
            strstr(out, "\"boot_id\":\"boot-a\"") != nullptr,
        "caller identity changes do not alter configured session");
}

void TestStatesAndFields() {
  TelemetryEncoder encoder;
  Check(encoder.begin("dog", "boot-a"), "state test configuration");
  int64_t expected_seq = 1;
  char out[kBufferSize];
  for (FsmState state : kStates) {
    auto sample = Sample();
    sample.state = state;
    sample.tipped = state == FsmState::Failsafe;
    sample.safety_latched = sample.tipped;
    const auto result = encoder.encode(sample, kEpoch, out, sizeof(out));
    Check(result.ok, "all 13 FSM states encode");
    Check(result.length == strlen(out), "reported output length excludes NUL");
    Check(encoder.next_seq() == ++expected_seq, "each valid encode advances sequence once");
    char state_field[64];
    snprintf(state_field, sizeof(state_field), "\"state\":\"%s\"", mechadog::to_string(state));
    Check(strstr(out, state_field) != nullptr, "wire FSM state matches command parser");
    Check(strchr(out, '\n') == nullptr, "one JSON object has no literal newline");
  }
  Check(strstr(out, "\"dist_cm\":47") != nullptr &&
            strstr(out, "\"imu\":{\"pitch\":1.25,\"roll\":-0.5,\"yaw\":183.5}") != nullptr &&
            strstr(out, "\"batt_v\":7.625") != nullptr &&
            strstr(out, "\"last_cmd_age_ms\":34") != nullptr &&
            strstr(out,
                   "\"flags\":{\"lowbatt\":false,\"tipped\":false,\"link_ok\":true,\"obstacle\":"
                   "false}") != nullptr &&
            strstr(out, "\"safety_latched\":false") != nullptr,
        "sample values and safety flags are serialized in their contract positions");

  auto sample = Sample();
  sample.lowbatt = true;
  Check(encoder.encode(sample, kEpoch, out, sizeof(out)).ok,
        "low battery warning may coexist with PATROL");
  sample.tipped = true;
  ExpectRejected(encoder, sample, "tipped PATROL contradicts telemetry contract");
  sample.state = FsmState::Failsafe;
  sample.safety_latched = true;
  sample.obstacle = true;
  sample.link_ok = false;
  Check(encoder.encode(sample, kEpoch, out, sizeof(out)).ok, "tipped FAILSAFE accepted");
  Check(strstr(out, "\"link_ok\":false,\"obstacle\":true") != nullptr &&
            strstr(out, "\"safety_latched\":true") != nullptr,
        "true and false safety flags preserved");
  sample = Sample();
  sample.state = FsmState::Unknown;
  ExpectRejected(encoder, sample, "unknown state refused");
  sample.state = static_cast<FsmState>(255);
  ExpectRejected(encoder, sample, "invalid state enum refused");
}

void TestOptionalObstacle() {
  TelemetryEncoder encoder;
  Check(encoder.begin("dog", "boot-optional"), "optional obstacle test configuration");
  TelemetrySample sample = Sample();
  char out[kBufferSize];
  Check(encoder.encode(sample, kEpoch, out, sizeof(out)).ok, "default obstacle report encodes");
  Check(strstr(out, "\"link_ok\":true,\"obstacle\":false}") != nullptr,
        "default callers retain the explicit false obstacle report");

  sample.include_obstacle = false;
  sample.obstacle = true;
  const auto omitted = encoder.encode(sample, kEpoch, out, sizeof(out));
  Check(omitted.ok && omitted.length == strlen(out), "omitted obstacle produces a complete packet");
  Check(strstr(out, "\"obstacle\"") == nullptr,
        "unavailable obstacle reporting omits the key even when its value is true");
  Check(strstr(out,
               "\"flags\":{\"lowbatt\":false,\"tipped\":false,\"link_ok\":true},"
               "\"safety_latched\":false}") != nullptr,
        "omission preserves required flags and JSON punctuation");

  sample.include_obstacle = true;
  Check(encoder.encode(sample, kEpoch, out, sizeof(out)).ok, "explicit obstacle report encodes");
  Check(strstr(out, "\"link_ok\":true,\"obstacle\":true}") != nullptr,
        "explicit inclusion preserves a true obstacle report");
  Check(encoder.next_seq() == 4, "optional field choice does not change packet sequencing");
}

void TestNumericValidation() {
  TelemetryEncoder encoder;
  Check(encoder.begin("dog", "boot"), "numeric test configuration");
  ExpectRejected(encoder, TelemetrySample{}, "default invalid sensor sample refused");
  auto sample = Sample();
  sample.sensors_valid = false;
  ExpectRejected(encoder, sample, "explicit stale/unavailable sensor sample refused");
  using MeasurementField = double TelemetrySample::*;
  const MeasurementField fields[] = {&TelemetrySample::dist_cm, &TelemetrySample::pitch,
                                     &TelemetrySample::roll, &TelemetrySample::yaw,
                                     &TelemetrySample::batt_v};
  const double invalid_numbers[] = {NAN, INFINITY, -INFINITY};
  for (auto field : fields) {
    for (double value : invalid_numbers) {
      sample = Sample();
      sample.*field = value;
      ExpectRejected(encoder, sample, "NaN or infinite measurement refused");
    }
  }
  sample = Sample();
  sample.dist_cm = -0.01;
  ExpectRejected(encoder, sample, "negative distance refused");
  sample = Sample();
  sample.batt_v = nextafter(6.0, 0.0);
  ExpectRejected(encoder, sample, "battery just below 6.0 refused");
  sample.batt_v = nextafter(8.6, 9.0);
  ExpectRejected(encoder, sample, "battery just above 8.6 refused");
  sample = Sample();
  sample.yaw = -0.01;
  ExpectRejected(encoder, sample, "negative yaw refused");
  sample.yaw = 360.0;
  ExpectRejected(encoder, sample, "yaw 360 excluded");
  sample = Sample();
  sample.last_cmd_age_ms = -1;
  ExpectRejected(encoder, sample, "negative command age refused");
  sample.last_cmd_age_ms = TelemetryEncoder::kMaxWireInteger + 1;
  ExpectRejected(encoder, sample, "inexact command age refused");
  ExpectRejected(encoder, Sample(), "negative timestamp refused", -1);
  ExpectRejected(encoder, Sample(), "inexact timestamp refused",
                 TelemetryEncoder::kMaxWireInteger + 1);
  sample = Sample();
  sample.batt_v = 6.0;
  sample.dist_cm = 0.0;
  sample.yaw = 0.0;
  sample.last_cmd_age_ms = 0;
  char out[kBufferSize];
  Check(encoder.encode(sample, 0, out, sizeof(out)).ok, "inclusive lower numeric bounds accepted");
  sample.batt_v = 8.6;  // Full charge plus measurement margin (ADR-30).
  sample.dist_cm = DBL_MAX;
  sample.pitch = -DBL_MAX;
  sample.roll = DBL_MAX;
  sample.yaw = nextafter(360.0, 0.0);
  sample.last_cmd_age_ms = TelemetryEncoder::kMaxWireInteger;
  Check(encoder.encode(sample, TelemetryEncoder::kMaxWireInteger, out, sizeof(out)).ok,
        "upper numeric bounds and finite double extrema accepted");
}

void TestEscapingAndBuffers() {
  TelemetryEncoder encoder;
  Check(encoder.begin("dog\"\\\n\t\x01\xea\xb0\x80", "boot\"\\\x1f"),
        "UTF-8 and escaped identities accepted");
  char out[kBufferSize];
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok, "escaped identity encoded");
  Check(strstr(out, "dog\\\"\\\\\\u000a\\u0009\\u0001\xea\xb0\x80") != nullptr,
        "quotes, backslashes, controls escaped and UTF-8 preserved");
  Check(strchr(out, '\n') == nullptr && strchr(out, '\t') == nullptr,
        "identity controls never split JSON lines");
  char control_identity[65];
  memset(control_identity, '\x01', 64);
  control_identity[64] = '\0';
  Check(encoder.begin(control_identity, control_identity), "maximal escaped identities accepted");
  const auto baseline = encoder.encode(Sample(), kEpoch, out, sizeof(out));
  Check(baseline.ok, "maximal escaping fits a sufficiently large caller buffer");
  for (size_t capacity = 0; capacity <= baseline.length + 1; ++capacity) {
    Check(encoder.begin(control_identity, control_identity), "reset buffer test session");
    memset(out, 'Z', sizeof(out));
    const auto result = encoder.encode(Sample(), kEpoch, out, capacity);
    Check(result.ok == (capacity == baseline.length + 1), "capacity includes terminator exactly");
    Check(out[capacity] == 'Z', "writer does not touch first byte outside capacity");
    Check(encoder.next_seq() == (result.ok ? 2 : 1), "buffer failure preserves sequence");
    if (!result.ok && capacity > 0) Check(out[0] == '\0', "short buffer output cleared");
  }
  const int64_t seq = encoder.next_seq();
  Check(!encoder.encode(Sample(), kEpoch, nullptr, 10).ok, "null output buffer refused");
  Check(encoder.next_seq() == seq, "null output does not consume sequence");
}

void TestSequenceAndReboot() {
  TelemetryEncoder encoder;
  Check(encoder.begin("dog", "boot-a"), "first boot configured");
  char out[kBufferSize];
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok && strstr(out, "{\"seq\":1,") == out,
        "first boot begins at sequence one");
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok && strstr(out, "{\"seq\":2,") == out,
        "next packet advances independently of command sequence");
  Check(encoder.begin("dog", "boot-b"), "new boot session configured");
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok &&
            strstr(out, "{\"seq\":1,") == out && strstr(out, "\"boot_id\":\"boot-b\"") != nullptr,
        "new boot session restarts sequence one");
  Check(encoder.begin("dog", "boot-limit", TelemetryEncoder::kMaxWireInteger),
        "last exact sequence configured");
  Check(encoder.encode(Sample(), kEpoch, out, sizeof(out)).ok &&
            strstr(out, "{\"seq\":9007199254740991,") == out,
        "last exact sequence transmitted without loss");
  ExpectRejected(encoder, Sample(), "sequence exhaustion refuses wraparound");
}

void TestLocale() {
  // Production ESP32 uses the C locale. A non-C host runtime must either keep
  // JSON decimal points or reject formatting without publishing a packet.
  const char* candidates[] = {"de_DE.UTF-8", "de_DE", "German_Germany.1252"};
  bool exercised = false;
  for (const char* locale : candidates) {
    if (setlocale(LC_NUMERIC, locale) == nullptr) continue;
    TelemetryEncoder encoder;
    Check(encoder.begin("dog", "locale"), "locale test configuration");
    char out[kBufferSize];
    const auto result = encoder.encode(Sample(), kEpoch, out, sizeof(out));
    if (result.ok) {
      Check(strstr(out, "\"batt_v\":7.625") != nullptr && encoder.next_seq() == 2,
            "non-C locale emits JSON decimal points");
    } else {
      Check(result.length == 0 && out[0] == '\0' && encoder.next_seq() == 1,
            "non-JSON numeric formatting fails without consuming sequence");
    }
    exercised = true;
    break;
  }
  Check(setlocale(LC_NUMERIC, "C") != nullptr, "restore C numeric locale");
  if (!exercised) puts("[SKIP] alternate numeric locale unavailable on this runtime");
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && strcmp(argv[1], "--emit-fixtures") == 0) return EmitFixtures();
  if (argc == 2 && strcmp(argv[1], "--emit-optional-fixtures") == 0) return EmitOptionalFixtures();
  TestConfiguration();
  TestStatesAndFields();
  TestOptionalObstacle();
  TestNumericValidation();
  TestEscapingAndBuffers();
  TestSequenceAndReboot();
  TestLocale();
  printf("Telemetry encoder: %d checks, %d failures\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
