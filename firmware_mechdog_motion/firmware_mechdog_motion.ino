#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <freertos/queue.h>

#include "src/command_parser.h"
#include "src/motion_hal.h"
#include "src/sensor_hal.h"
#include "src/telemetry_publisher.h"

#if defined(MECHADOG_WIFI_SSID) != defined(MECHADOG_WIFI_PASSWORD)
#error "Provide both private Wi-Fi build settings, or neither to use saved NVS settings."
#endif

namespace {

constexpr uint16_t kCommandPort = 5001;
constexpr uint32_t kCommandTimeoutMs = 300;
constexpr uint32_t kReconnectIntervalMs = 3000;
constexpr uint32_t kSensorStatusLogIntervalMs = 1000;
constexpr size_t kSerialTxBufferBytes = 1024;
// config/config.yaml safety.link_loss_failsafe_ms and battery_warn_v.
// These report link health and a warning; they add no new shutdown behavior.
constexpr uint32_t kLinkHealthyAgeMs = 3000;
constexpr float kBatteryWarningV = 7.0f;
constexpr size_t kPacketBufferSize = 512;

WiFiUDP g_udp;
mechadog::CommandParser g_parser;
mechadog::MotionHal g_motion;
mechadog::SensorHal g_sensors;
mechadog::TelemetryPublisher g_telemetry;

char g_packet[kPacketBufferSize + 1];
bool g_safe_latched = true;
bool g_have_valid_command = false;
uint64_t g_last_valid_command_ms = 0;
uint64_t g_next_sensor_status_log_ms = 0;
uint64_t g_previous_loop_us = 0;
uint64_t g_max_loop_gap_us = 0;
uint32_t g_sensor_status_dropped = 0;
uint32_t g_last_reconnect_attempt_ms = 0;
uint32_t g_failsafe_count = 0;
bool g_udp_started = false;
bool g_wifi_connected = false;
mechadog::FsmState g_reported_state = mechadog::FsmState::Idle;

struct WifiDiagnosticEvent {
  uint32_t callback_ms;
  uint32_t event;
  uint8_t reason;
};
constexpr size_t kWifiDiagnosticQueueLength = 16;
StaticQueue_t g_wifi_diagnostic_queue_storage;
uint8_t g_wifi_diagnostic_queue_bytes[kWifiDiagnosticQueueLength * sizeof(WifiDiagnosticEvent)];
QueueHandle_t g_wifi_diagnostic_queue = nullptr;
uint32_t g_wifi_diagnostic_dropped = 0;
uint32_t g_connect_attempt_count = 0;

void onWifiDiagnosticEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (event != ARDUINO_EVENT_WIFI_STA_START && event != ARDUINO_EVENT_WIFI_STA_CONNECTED &&
      event != ARDUINO_EVENT_WIFI_STA_DISCONNECTED && event != ARDUINO_EVENT_WIFI_STA_GOT_IP &&
      event != ARDUINO_EVENT_WIFI_STA_LOST_IP) {
    return;
  }
  // Arduino dispatches this after its own reconnect handler. This is callback
  // delivery time, not the physical event timestamp. Never do I/O or reconnect
  // from this callback; its bounded queue is drained by loop().
  const WifiDiagnosticEvent record = {millis(), static_cast<uint32_t>(event),
                                      event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED
                                          ? info.wifi_sta_disconnected.reason
                                          : uint8_t{0}};
  if (g_wifi_diagnostic_queue != nullptr &&
      xQueueSend(g_wifi_diagnostic_queue, &record, 0) != pdTRUE) {
    __atomic_fetch_add(&g_wifi_diagnostic_dropped, 1, __ATOMIC_RELAXED);
  }
}

void pollWifiDiagnostics() {
  WifiDiagnosticEvent event;
  // Keep the oldest pending event until UART has room; limit work to one event
  // per loop. Queue overflow is explicit and does not alter sensor acquisition.
  if (g_wifi_diagnostic_queue == nullptr ||
      xQueuePeek(g_wifi_diagnostic_queue, &event, 0) != pdTRUE) {
    return;
  }
  char text[192];
  const int length = snprintf(
      text, sizeof(text), "Wi-Fi event: callback_ms=%lu event=%lu reason=%u dropped=%lu\n",
      static_cast<unsigned long>(event.callback_ms), static_cast<unsigned long>(event.event),
      static_cast<unsigned>(event.reason),
      static_cast<unsigned long>(__atomic_load_n(&g_wifi_diagnostic_dropped, __ATOMIC_RELAXED)));
  if (length > 0 && static_cast<size_t>(length) < sizeof(text) &&
      Serial.availableForWrite() >= length + 128) {
    Serial.write(reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(length));
    xQueueReceive(g_wifi_diagnostic_queue, &event, 0);
  }
}

void retryWifiWithoutDisconnect() {
  // WiFi.reconnect() in Arduino-ESP32 2.0.12 first calls esp_wifi_disconnect().
  // Doing that every three seconds can interrupt association or DHCP. Preserve
  // an associated AP and ask the driver to connect without tearing it down.
  wifi_ap_record_t ap;
  if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) return;
  const uint64_t started_us = static_cast<uint64_t>(esp_timer_get_time());
  const esp_err_t result = esp_wifi_connect();
  const uint64_t elapsed_us = static_cast<uint64_t>(esp_timer_get_time()) - started_us;
  ++g_connect_attempt_count;
  char text[192];
  const int length =
      snprintf(text, sizeof(text), "Wi-Fi retry: at_ms=%llu count=%lu result=%ld elapsed_us=%llu\n",
               static_cast<unsigned long long>(started_us / 1000),
               static_cast<unsigned long>(g_connect_attempt_count), static_cast<long>(result),
               static_cast<unsigned long long>(elapsed_us));
  if (length > 0 && static_cast<size_t>(length) < sizeof(text) &&
      Serial.availableForWrite() >= length + 128) {
    Serial.write(reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(length));
  }
}

uint64_t uptimeMs() {
  return static_cast<uint64_t>(esp_timer_get_time()) / 1000;
}

void sendText(const char* text) {
  g_udp.beginPacket(g_udp.remoteIP(), g_udp.remotePort());
  g_udp.write(reinterpret_cast<const uint8_t*>(text), strlen(text));
  g_udp.endPacket();
}

void sendAck(const mechadog::DecodeResult& decoded, bool applied) {
  char response[240];
  snprintf(response, sizeof(response),
           "{\"ok\":%s,\"verdict\":\"%s\",\"seq\":%lld,\"type\":\"%s\","
           "\"applied\":%s,\"safe_latched\":%s,\"failsafe_count\":%lu,"
           "\"actuators\":%s}",
           decoded.verdict == mechadog::Verdict::Accept ? "true" : "false",
           mechadog::to_string(decoded.verdict), static_cast<long long>(decoded.command.seq),
           mechadog::to_string(decoded.command.type), applied ? "true" : "false",
           g_safe_latched ? "true" : "false", static_cast<unsigned long>(g_failsafe_count),
           g_motion.actuators_enabled() ? "true" : "false");
  sendText(response);
}

void latchFailsafe(const char* reason) {
  if (!g_safe_latched) {
    ++g_failsafe_count;
    Serial.printf("FAILSAFE: %s\n", reason);
  }
  g_safe_latched = true;
  g_motion.stop();
}

bool applyCommand(const mechadog::Command& command) {
  switch (command.type) {
    case mechadog::CmdType::Stop:
      g_motion.stop();
      return true;

    case mechadog::CmdType::Estop:
      latchFailsafe("ESTOP command");
      return true;

    case mechadog::CmdType::ResetSafe:
      if (WiFi.status() != WL_CONNECTED) return false;
      g_motion.stop();
      g_safe_latched = false;
      g_reported_state = mechadog::FsmState::Idle;
      Serial.println("SAFE latch cleared; waiting for a new MOVE");
      return true;

    case mechadog::CmdType::Move:
      if (g_safe_latched) return false;
      g_motion.move(command.step, command.angle);
      return true;

    case mechadog::CmdType::State:
      // Host FSM state is stored and echoed, never used to clear a safety latch.
      g_reported_state = command.state;
      return true;

    default:
      // Parsed safely, but this firmware stage does not drive these features yet.
      return false;
  }
}

void handlePacket(int packet_size) {
  if (packet_size <= 0) return;

  const size_t wanted = packet_size > static_cast<int>(kPacketBufferSize)
                            ? kPacketBufferSize
                            : static_cast<size_t>(packet_size);
  const int read_len = g_udp.read(g_packet, wanted);
  while (g_udp.available()) g_udp.read();
  if (read_len <= 0) return;
  g_packet[read_len] = '\0';

  // Diagnostic probes are deliberately outside the command watchdog.
  if (read_len >= 4 && memcmp(g_packet, "PING", 4) == 0) {
    char response[kPacketBufferSize + 5];
    memcpy(response, "ACK ", 4);
    memcpy(response + 4, g_packet, read_len);
    response[read_len + 4] = '\0';
    sendText(response);
    return;
  }

  if (packet_size > static_cast<int>(kPacketBufferSize)) {
    mechadog::DecodeResult rejected;
    rejected.verdict = mechadog::Verdict::Discard;
    sendAck(rejected, false);
    return;
  }

  const mechadog::DecodeResult decoded = g_parser.decode(g_packet, read_len);
  if (decoded.verdict != mechadog::Verdict::Accept) {
    Serial.printf("DROP: %s (%s)\n", mechadog::to_string(decoded.verdict), decoded.reason);
    sendAck(decoded, false);
    return;
  }

  g_have_valid_command = true;
  g_last_valid_command_ms = uptimeMs();
  // Only an accepted protocol command identifies the telemetry destination and
  // anchors its epoch clock. PINGs, malformed packets and replay drops do not.
  g_telemetry.observe_command(g_udp.remoteIP(), decoded.command.ts);
  const bool applied = applyCommand(decoded.command);
  Serial.printf("CMD: seq=%lld type=%s applied=%d safe=%d\n",
                static_cast<long long>(decoded.command.seq),
                mechadog::to_string(decoded.command.type), applied, g_safe_latched);
  sendAck(decoded, applied);
}

void connectSavedWifi() {
  g_wifi_diagnostic_queue =
      xQueueCreateStatic(kWifiDiagnosticQueueLength, sizeof(WifiDiagnosticEvent),
                         g_wifi_diagnostic_queue_bytes, &g_wifi_diagnostic_queue_storage);
  WiFi.onEvent(onWifiDiagnosticEvent);
  WiFi.mode(WIFI_STA);
  // Command latency matters more than power saving on the body MCU. Modem sleep
  // can delay UDP bursts long enough to trip the 300 ms motion watchdog.
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
#if defined(MECHADOG_WIFI_SSID) && defined(MECHADOG_WIFI_PASSWORD)
  // Optional private build settings provision a board without saved credentials.
  // Never print these values or place the private header in the repository.
  WiFi.begin(MECHADOG_WIFI_SSID, MECHADOG_WIFI_PASSWORD);
#else
  WiFi.begin();  // Reuse the router credentials already saved in ESP32 NVS.
#endif
  g_last_reconnect_attempt_ms = millis();
  Serial.println("Connecting to saved Wi-Fi; SAFE latch remains ON");
}

void startUdpIfNeeded() {
  if (g_udp_started || WiFi.status() != WL_CONNECTED) return;
  g_udp_started = g_udp.begin(kCommandPort) != 0;
  if (g_udp_started) {
    Serial.printf("UDP port: %u\n", kCommandPort);
  } else {
    Serial.println("UDP bind failed; will retry");
  }
}

void pollTelemetry() {
  const mechadog::SensorSnapshot sensors = g_sensors.snapshot(millis());
  const uint64_t now = uptimeMs();
  const uint64_t command_age = now - g_last_valid_command_ms;
  mechadog::TelemetrySample sample;
  // Acquisition validity/freshness does not certify body axes or voltage
  // calibration. Sensors may now run with actuators (sensor_hal.h I2C rule);
  // verified walking in the air on 2026-09-12, floor walking still pending.
  sample.sensors_valid = sensors.all_valid();
  sample.state = g_safe_latched ? mechadog::FsmState::Failsafe : g_reported_state;
  sample.dist_cm = sensors.dist_cm;
  sample.pitch = sensors.pitch;
  sample.roll = sensors.roll;
  sample.yaw = sensors.yaw;
  sample.batt_v = sensors.batt_v;
  sample.last_cmd_age_ms = static_cast<int64_t>(command_age);
  sample.lowbatt = sensors.batt_valid && sensors.batt_v <= kBatteryWarningV;
  // Fall detection is not implemented: false means no onboard tipped-stop has
  // been activated. It does NOT establish a verified upright posture.
  sample.tipped = false;
  sample.link_ok = g_have_valid_command && command_age <= kLinkHealthyAgeMs;
  // No onboard obstacle-stop detector is connected in this firmware stage.
  sample.include_obstacle = false;
  sample.safety_latched = g_safe_latched;
  // Publisher enforces 100 ms cadence and rejects missing/invalid sensor data.
  g_telemetry.poll(sample);

  if (now >= g_next_sensor_status_log_ms) {
    g_next_sensor_status_log_ms = now + kSensorStatusLogIntervalMs;
    // Snapshot values are diagnostic context; invalid values are not certified
    // measurements. Report validity/errors alongside them without changing flags.
    char status[768];
    const int length = snprintf(
        status, sizeof(status),
        "Sensor status: task_started=%u imu_valid=%u imu_error=%s dist_valid=%u dist_error=%s "
        "batt_valid=%u batt_error=%s battery_raw=%u adc_mv=%lu adc_source=%s "
        "pitch=%.3f roll=%.3f yaw=%.3f "
        "dist_cm=%.3f batt_v=%.3f loop_gap_max_us=%llu heap_free=%u heap_min=%u log_dropped=%lu "
        "imu_timing=%s imu_fault_ms=%lu imu_wake_late_ms=%lu imu_cycle_ms=%lu "
        "imu_stage_mask=%u imu_filter_us=%llu sonar_us=%llu adc_us=%llu\n",
        static_cast<unsigned>(sensors.task_started), static_cast<unsigned>(sensors.imu_valid),
        mechadog::sensor_error_name(sensors.imu_error), static_cast<unsigned>(sensors.dist_valid),
        mechadog::sensor_error_name(sensors.dist_error), static_cast<unsigned>(sensors.batt_valid),
        mechadog::sensor_error_name(sensors.batt_error), static_cast<unsigned>(sensors.battery_raw),
        static_cast<unsigned long>(sensors.battery_adc_mv),
        mechadog::battery_adc_calibration_name(sensors.battery_adc_calibration), sensors.pitch,
        sensors.roll, sensors.yaw, sensors.dist_cm, sensors.batt_v,
        static_cast<unsigned long long>(g_max_loop_gap_us), ESP.getFreeHeap(), ESP.getMinFreeHeap(),
        static_cast<unsigned long>(g_sensor_status_dropped),
        mechadog::sensor_timing_fault_reason_name(sensors.timing_fault.reason),
        static_cast<unsigned long>(sensors.timing_fault.fault_tick_ms),
        static_cast<unsigned long>(sensors.timing_fault.wake_late_ms),
        static_cast<unsigned long>(sensors.timing_fault.cycle_elapsed_ms),
        static_cast<unsigned>(sensors.timing_fault.stages_measured_mask),
        static_cast<unsigned long long>(sensors.timing_fault.imu_filter_us),
        static_cast<unsigned long long>(sensors.timing_fault.sonar_us),
        static_cast<unsigned long long>(sensors.timing_fault.adc_us));
    // Leave room for the ESP32 FIFO counted by availableForWrite as well as
    // the whole line in the TX ring. Logging is best-effort: other startup
    // writers may still contend. Only diagnostics are dropped, never samples.
    if (length > 0 && static_cast<size_t>(length) < sizeof(status) &&
        Serial.availableForWrite() >= length + 128) {
      Serial.write(reinterpret_cast<const uint8_t*>(status), static_cast<size_t>(length));
      g_max_loop_gap_us = 0;
    } else {
      ++g_sensor_status_dropped;
    }
  }
}

}  // namespace

void setup() {
  // Arduino-ESP32 2.0.12 otherwise defaults to an unbuffered UART transmitter.
  Serial.setTxBufferSize(kSerialTxBufferBytes);
  Serial.begin(115200);
  delay(300);
  Serial.println("MechDog command receiver booting");
  Serial.printf("Runtime: cpu_mhz=%u uart_tx_buffer=%u\n", getCpuFrequencyMhz(),
                static_cast<unsigned>(kSerialTxBufferBytes));

  g_motion.begin();
  g_motion.stop();
  const bool sensors_started = g_sensors.begin();
  Serial.printf("Sensors: enabled=%d task_started=%d\n", g_sensors.enabled(), sensors_started);
  if (g_sensors.enabled()) {
    Serial.println("Sensor diagnostics: body axes unverified, yaw relative, battery uncalibrated");
  }
  connectSavedWifi();
  if (!g_telemetry.begin()) {
    Serial.println("Telemetry initialization failed; command receiver remains available");
  }
  Serial.printf("Actuators: %s, initial SAFE latch: ON\n",
                g_motion.actuators_enabled() ? "ON" : "OFF");
}

void loop() {
  const uint64_t loop_us = static_cast<uint64_t>(esp_timer_get_time());
  if (g_previous_loop_us != 0 && loop_us - g_previous_loop_us > g_max_loop_gap_us) {
    g_max_loop_gap_us = loop_us - g_previous_loop_us;
  }
  g_previous_loop_us = loop_us;
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    // WiFiUDP의 로컬 소켓은 인터페이스가 끊긴 뒤에도 started 상태로 남을 수
    // 있다. 명시적으로 닫아야 재접속 뒤 같은 포트에 다시 bind한다.
    if (g_wifi_connected) {
      if (g_udp_started) {
        g_udp.stop();
        g_udp_started = false;
      }
      // Clear publisher socket/peer once per disconnect, not every loop tick.
      g_telemetry.disconnected();
      g_wifi_connected = false;
      Serial.println("UDP stopped; waiting for Wi-Fi reconnect");
    }
    latchFailsafe("Wi-Fi disconnected");
    if (now - g_last_reconnect_attempt_ms >= kReconnectIntervalMs) {
      g_last_reconnect_attempt_ms = now;
      retryWifiWithoutDisconnect();
    }
  } else {
    if (!g_wifi_connected) {
      g_wifi_connected = true;
      Serial.printf("Wi-Fi connected: %s RSSI=%d\n", WiFi.localIP().toString().c_str(),
                    WiFi.RSSI());
    }
    startUdpIfNeeded();
    const int packet_size = g_udp.parsePacket();
    if (packet_size > 0) handlePacket(packet_size);
  }

  // Read monotonic time again after packet handling. The command timestamp can
  // be newer than the loop's earlier capture; keep the age in 64-bit uptime.
  const uint64_t watchdog_now = uptimeMs();
  if (g_have_valid_command && !g_safe_latched &&
      watchdog_now - g_last_valid_command_ms >= kCommandTimeoutMs) {
    latchFailsafe("command timeout >= 300 ms");
  }

  // Safety decisions precede acquisition snapshot and telemetry publication.
  pollTelemetry();
  pollWifiDiagnostics();
  delay(1);
}
