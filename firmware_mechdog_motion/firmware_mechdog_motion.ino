#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <freertos/queue.h>

#include "src/command_parser.h"
#include "src/motion_hal.h"
#include "src/motion_safety_state.h"
#include "src/safety_monitor.h"
#include "src/sensor_hal.h"
#include "src/stationary_ota.h"
#include "src/telemetry_publisher.h"

#ifndef MECHADOG_ENABLE_TASK_WDT
#define MECHADOG_ENABLE_TASK_WDT 0
#endif
#ifndef MECHADOG_WATCHDOG_FAULT_PROBE
#define MECHADOG_WATCHDOG_FAULT_PROBE 0
#endif
#if MECHADOG_WATCHDOG_FAULT_PROBE && !MECHADOG_ENABLE_TASK_WDT
#error "Fault probe requires the independent watchdog and an actuator-OFF bench"
#endif
#if MECHADOG_ENABLE_TASK_WDT
#include <esp_system.h>

#include "src/task_watchdog.h"
// Vendor startup can move the body after a reset, so an actuator build never
// arms the watchdog at boot. Instead it exposes SERVICE mode: entering parks
// the body and blocks motion first, then arms the deadline — a watchdog
// restart can therefore only fire while the robot is already standing still,
// which is the same vendor-init motion as every normal boot (physically
// verified on gate-20260913). Reboot-while-walking stays structurally absent.
#if MECHADOG_ENABLE_ACTUATORS
#define MECHADOG_SERVICE_MODE 1
#else
#define MECHADOG_SERVICE_MODE 0
#endif
// Stationary OTA uses its own task. The loop deadline remains active during
// transfers, confirmation and flash operations; there is no maintenance feed.
#endif

#ifndef MECHADOG_SERVICE_MODE
#define MECHADOG_SERVICE_MODE 0
#endif

// ⚠️ **벤치 전용 — 기본 빌드에는 절대 켜지 않는다.** 가변전원이 없으면 7.0/6.6V
// 교차를 만들 수 없으므로, 만충 근처에서 교차가 생기도록 임계만 올려 `3.2.2` 의
// 동작(플래그 전환 · 셧다운 래치)을 실기에서 확인한다. 판정 코드는 정식 빌드와
// 같은 경로이며, 확인이 끝나면 이 플래그를 끄고 다시 올린다.
#ifndef MECHADOG_BENCH_BATTERY_THRESHOLDS
#define MECHADOG_BENCH_BATTERY_THRESHOLDS 0
#endif
// 값은 그날 배터리에 맞춰 빌드 때 준다 — ⚠️ **부하 강하를 빼고 고르면 안 된다.**
// 실측(2026-09-17 · mechdog-01)에서 정지 8.32V 가 보행 중 8.04V 까지 내려앉았다.
#ifndef MECHADOG_BENCH_BATTERY_WARN_V
#define MECHADOG_BENCH_BATTERY_WARN_V 8.2f
#endif
#ifndef MECHADOG_BENCH_BATTERY_SHUTDOWN_V
#define MECHADOG_BENCH_BATTERY_SHUTDOWN_V 8.0f
#endif

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
// Tier 1 온보드 판정 임계 — config/config.yaml `safety` 절과 같은 값이다.
constexpr float kBatteryShutdownV = 6.6f;  // safety.battery_shutdown_v (NFR-2.3)
constexpr float kObstacleStopCm = 25.0f;   // safety.obstacle_stop_cm (FR-2.2)
constexpr float kObstacleClearCm = 30.0f;  // 해제는 더 멀리서 — 경계 진동 방지
constexpr size_t kPacketBufferSize = 512;

WiFiUDP g_udp;
mechadog::CommandParser g_parser;
mechadog::MotionHal g_motion;
mechadog::SensorHal g_sensors;
mechadog::TelemetryPublisher g_telemetry;

// ── 눈 LED (FR-10.4 · WBS 4.7.3) ─────────────────────────────────────────────
// 호스트가 에스컬레이션 단계를 색으로 내려보내고(L0~L3), 래치가 걸리면 온보드가
// 흰색으로 덮는다.
//
// ⚠️ **흰색을 호스트에 맡길 수 없다.** F 로 가는 원인 하나가 링크 두절인데, 그때는
// 호스트가 아무것도 보낼 수 없다. 2026-09-18 점검에서 기체가 정확히 그 상태였다 —
// Wi-Fi 가 끊겨 래치가 걸린 채였고 호스트는 색을 보낼 방법이 없었다.
struct EyeColor {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

struct EyeNamedColor {
  const char* name;
  EyeColor color;
};

constexpr EyeColor kEyeWhite = {255, 255, 255};
constexpr EyeColor kEyeOff = {0, 0, 0};

// `config.yaml` 의 `escalation.led` 다섯 색만 안다. 모르는 색은 받지 않는다 —
// 임의 색으로 바꾸거나 꺼 버리면 «LED 고장» 과 구별할 수 없다.
constexpr EyeNamedColor kEyeColors[] = {
    {"blue", {0, 0, 255}}, {"yellow", {255, 255, 0}}, {"orange", {255, 128, 0}},
    {"red", {255, 0, 0}},  {"white", kEyeWhite},
};

EyeColor g_eye_commanded = {0, 0, 255};  // 호스트가 지시한 색. 기본은 L0 파랑이다.
float g_eye_blink_hz = 0.0f;             // 0 이면 상시 점등. 규약상 L3 에만 붙는다.
bool g_eye_blink_on = true;
uint32_t g_eye_blink_toggled_ms = 0;
bool g_eye_written = false;
EyeColor g_eye_written_color = kEyeOff;

mechadog::SafetyMonitor makeSafetyMonitor() {
  mechadog::SafetyThresholds thresholds;
  thresholds.obstacle_stop_cm = kObstacleStopCm;
  thresholds.obstacle_clear_cm = kObstacleClearCm;
  thresholds.battery_warn_v = kBatteryWarningV;
  thresholds.battery_shutdown_v = kBatteryShutdownV;
#if MECHADOG_BENCH_BATTERY_THRESHOLDS
  // 만충 근처에서 교차가 생기도록 올린다 (벤치 전용).
  thresholds.battery_warn_v = MECHADOG_BENCH_BATTERY_WARN_V;
  thresholds.battery_shutdown_v = MECHADOG_BENCH_BATTERY_SHUTDOWN_V;
#endif
  return mechadog::SafetyMonitor(thresholds);
}

mechadog::SafetyMonitor g_safety = makeSafetyMonitor();

char g_packet[kPacketBufferSize + 1];
mechadog::MotionSafetyState g_motion_state;
bool g_have_valid_command = false;
uint64_t g_last_valid_command_ms = 0;
uint64_t g_next_sensor_status_log_ms = 0;
uint64_t g_previous_loop_us = 0;
uint64_t g_max_loop_gap_us = 0;
uint32_t g_sensor_status_dropped = 0;
uint64_t g_next_sensor_performance_log_ms = 500;
uint32_t g_sensor_performance_dropped = 0;
uint8_t g_sensor_performance_metric = 0;
uint32_t g_last_reconnect_attempt_ms = 0;
uint32_t g_failsafe_count = 0;
bool g_udp_started = false;
bool g_wifi_connected = false;
mechadog::FsmState g_reported_state = mechadog::FsmState::Idle;

#if MECHADOG_SERVICE_MODE
// SERVICE mode parks the body and arms the loop watchdog; the arming order is
// documented at the compile guard above. The carrier-board user button
// (vendor Hiwonder.cpp Key_Pin = GPIO5, active low, pull-up) toggles it
// without a host — useful when the board is on the bench for patching.
constexpr uint8_t kServiceButtonPin = 5;
constexpr uint32_t kServiceButtonDebounceMs = 50;
bool g_service_button_raw = true;  // pull-up: released reads HIGH
uint32_t g_service_button_since_ms = 0;
bool g_service_button_pressed = false;
#endif

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

bool serviceModeActive();
bool loopWatchdogArmed();

void sendAck(const mechadog::DecodeResult& decoded, bool applied) {
  char response[320];
  snprintf(response, sizeof(response),
           "{\"ok\":%s,\"verdict\":\"%s\",\"seq\":%lld,\"type\":\"%s\","
           "\"applied\":%s,\"safe_latched\":%s,\"failsafe_count\":%lu,"
           "\"actuators\":%s,\"service_mode\":%s,\"wdt_armed\":%s}",
           decoded.verdict == mechadog::Verdict::Accept ? "true" : "false",
           mechadog::to_string(decoded.verdict), static_cast<long long>(decoded.command.seq),
           mechadog::to_string(decoded.command.type), applied ? "true" : "false",
           g_motion_state.safe_latched ? "true" : "false",
           static_cast<unsigned long>(g_failsafe_count),
           g_motion.actuators_enabled() ? "true" : "false", serviceModeActive() ? "true" : "false",
           loopWatchdogArmed() ? "true" : "false");
  sendText(response);
}

void latchFailsafe(const char* reason) {
  if (!g_motion_state.safe_latched) {
    ++g_failsafe_count;
    Serial.printf("FAILSAFE: %s\n", reason);
  }
  g_motion_state.latch();
  g_motion.stop();
}

bool serviceModeActive() {
#if MECHADOG_SERVICE_MODE
  return g_motion_state.service_mode;
#else
  return false;
#endif
}

bool loopWatchdogArmed() {
#if MECHADOG_ENABLE_TASK_WDT
  return mechadog::taskWatchdogArmed();
#else
  return false;
#endif
}

#if MECHADOG_SERVICE_MODE
// Park first, arm second: motion is latched and blocked before the deadline is
// armed, so a watchdog restart can only happen to a standing robot. The safe
// latch is set directly rather than through latchFailsafe — entering service
// mode is a deliberate operator action, not a fault, so failsafe_count must
// not move.
bool enterServiceMode() {
  g_motion.stop();
  g_motion_state.enter_service();
  const esp_err_t err = mechadog::startTaskWatchdog();
  if (err == ESP_ERR_INVALID_STATE) {
    Serial.println("SERVICE mode: watchdog already armed");
    return true;
  }
  if (err != ESP_OK) {
    // The body is parked regardless; only the deadline arm failed. The host
    // sees wdt_armed=false in the ACK and telemetry.
    Serial.printf("SERVICE mode: watchdog arm failed err=%ld\n", static_cast<long>(err));
    return false;
  }
  Serial.println("SERVICE mode: motion blocked, loop watchdog armed");
  return true;
}

bool exitServiceMode() {
  if (!g_motion_state.service_mode) return false;
  const esp_err_t err = mechadog::disarmTaskWatchdog();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    Serial.printf("SERVICE exit: watchdog disarm failed err=%ld\n", static_cast<long>(err));
    return false;
  }
  // RESET_SAFE is accepted while SERVICE is active, so re-park and re-latch
  // explicitly before returning control to normal motion.
  g_motion.stop();
  g_motion_state.exit_service();
  Serial.println("SERVICE mode: exited; SAFE latch remains ON");
  return true;
}

void pollServiceButton() {
  const bool raw = digitalRead(kServiceButtonPin) == LOW;
  const uint32_t now = millis();
  if (raw != g_service_button_raw) {
    g_service_button_raw = raw;
    g_service_button_since_ms = now;
    return;
  }
  if (now - g_service_button_since_ms < kServiceButtonDebounceMs) return;
  if (raw == g_service_button_pressed) return;
  g_service_button_pressed = raw;
  if (!raw) return;  // press edge only; release carries no action
  if (g_motion_state.service_mode) {
    exitServiceMode();
  } else {
    enterServiceMode();
  }
}
#endif

bool eyeColorFor(const char* name, EyeColor& out) {
  for (const EyeNamedColor& entry : kEyeColors) {
    if (strcmp(entry.name, name) == 0) {
      out = entry.color;
      return true;
    }
  }
  return false;
}

// 색이 바뀔 때만 모듈에 쓴다. 매 루프 쓰면 초음파와 같은 버스를 계속 먹는다.
void pollEyeLed() {
  const uint32_t now = millis();
  EyeColor want = g_eye_commanded;
  bool blink = g_eye_blink_hz > 0.0f;
  // 래치 중에는 온보드가 흰색으로 덮고 점멸도 하지 않는다 — 아키텍처 3.1 표에서
  // 점멸은 L3 경보에만 붙는다. 페일세이프는 «켜져 있음» 으로 보여야 한다.
  if (g_motion_state.safe_latched) {
    want = kEyeWhite;
    blink = false;
  }
  if (blink) {
    // ⚠️ delay() 로 만들지 않는다. 그렇게 하면 UDP 수신도 300ms 명령 타임아웃 검사도
    // 함께 멈춘다 — `ACTION` 이 걷는 중에 거부되는 것과 같은 이유다.
    const uint32_t half_period_ms = static_cast<uint32_t>(500.0f / g_eye_blink_hz);
    if (now - g_eye_blink_toggled_ms >= half_period_ms) {
      g_eye_blink_on = !g_eye_blink_on;
      g_eye_blink_toggled_ms = now;
    }
    if (!g_eye_blink_on) want = kEyeOff;
  } else {
    g_eye_blink_on = true;
  }
  if (g_eye_written && want.r == g_eye_written_color.r && want.g == g_eye_written_color.g &&
      want.b == g_eye_written_color.b) {
    return;
  }
  // 실패하면 다음 루프에서 다시 시도한다. 표시가 실제 상태와 어긋난 채로 두지 않는다.
  if (mechadog::writeEyeLed(want.r, want.g, want.b)) {
    g_eye_written = true;
    g_eye_written_color = want;
  }
}

bool applyCommand(const mechadog::Command& command) {
  switch (command.type) {
    case mechadog::CmdType::Stop:
      g_motion_state.stop();
      g_motion.stop();
      return true;

    case mechadog::CmdType::Estop:
      latchFailsafe("ESTOP command");
      return true;

    case mechadog::CmdType::ResetSafe:
      if (WiFi.status() != WL_CONNECTED) return false;
      // ⚠️ **원인이 남아 있으면 풀지 않는다 (3.2.5).** 저전압으로 세운 기체를
      // 그대로 풀어 주면 다음 MOVE 에서 걷다가 또 걸린다 — 사람은 해제 버튼만
      // 반복해서 누르게 된다. 가상 로봇은 처음부터 이렇게 거부하고 있었고
      // (`mock_mechdog._physical_fault`) 펌웨어만 받아 주고 있었다.
      if (!mechadog::reset_safe_allowed(g_safety)) {
        Serial.println("RESET_SAFE refused: battery still below shutdown threshold");
        return false;
      }
      // A pending-verify OTA image must stay parked until confirmed: clearing
      // the latch would let motion start while the 90 s verify deadline (or
      // the confirm reboot) can still restart the robot mid-gait.
      if (mechadog::otaPendingVerify()) return false;
      g_motion.stop();
      // Clearing the latch while in service mode is accepted but motion stays
      // blocked by the service flag — RESET alone must not resume a parked
      // service robot. Exiting service mode re-latches anyway.
      g_motion_state.reset_safe();
      g_reported_state = mechadog::FsmState::Idle;
      Serial.println("SAFE latch cleared; waiting for a new MOVE");
      return true;

    case mechadog::CmdType::Move:
      // ⚠️ **우선순위는 `move_allowed` 한 곳에 있다 (3.2.5).** 래치·서비스 모드가
      // 먼저이고 그다음이 온보드 반사 정지 (3.2.6) 다. 반사 정지는 호스트 판단을
      // 기다리지 않으며 **전진만** 거부한다 — 후진·선회는 통과시켜야 FR-2.3 의
      // «정지 후 후진» 이 성립한다. 초음파는 정면만 보므로 물러나는 것이 유일한
      // 탈출로다.
      if (!mechadog::move_allowed(g_motion_state, g_safety, command.step)) return false;
      g_motion.move(command.step, command.angle);
      // 보행 중인지 기록한다. ACTION 가드가 이 값을 본다 — 호스트가 되돌려준
      // `state` 는 반향이라 쓰지 않는다(ADR-22).
      g_motion_state.note_move(command.step, command.angle);
      return true;

    case mechadog::CmdType::Pose:
      if (g_motion_state.safe_latched) return false;
      g_motion.pose(command.pitch, command.roll, command.height, static_cast<int>(command.dur));
      return true;

    case mechadog::CmdType::Action:
      // ⚠️ **걷는 중에는 받지 않는다.** 벤더 `action_run` 은 구간마다 delay() 로
      // 블로킹하므로 그동안 loop() 가 통째로 멈춘다 — UDP 수신도, 300ms 명령
      // 타임아웃 검사도 함께 멈춘다. 걷다가 멈추면 **타임아웃이 자기 자신 때문에
      // 걸린다.** 정지 상태에서만 1초를 감수한다 (NFR-1 비목표: 온보드 블로킹 금지).
      //
      // ⚠️ **SERVICE 중에도 받지 않는다.** 같은 1초 블로킹이 거기서는 재부팅이
      // 된다 — `enterServiceMode()` 가 루프 워치독을 무장시키고(750ms) 그 마감을
      // 넘기기 때문이다. 정비하려고 세워 둔 기체에서 일어나면 안 되는 일이다.
      if (!g_motion_state.can_action()) {
        Serial.println(g_motion_state.service_mode ? "ACTION refused: service mode"
                                                   : "ACTION refused: walking");
        return false;
      }
      // 래치 중에는 받는다 — `FAILSAFE` 안정 자세(엎드림)가 이 경로로 온다.
      return g_motion.action(static_cast<int>(command.action_id));

    case mechadog::CmdType::Service:
#if MECHADOG_SERVICE_MODE
      return command.service_mode == mechadog::ServiceMode::Enter ? enterServiceMode()
                                                                  : exitServiceMode();
#else
      return false;  // no runtime-armed watchdog in this build
#endif

    case mechadog::CmdType::Led: {
      EyeColor color;
      // 모르는 색은 폐기가 아니라 «적용 안 됨» 이다. 규약이 색을 문자열로 두었고,
      // 표시할 수 있는 것은 설정에 있는 다섯 색뿐이다. 직전 색은 그대로 둔다.
      if (!eyeColorFor(command.color, color)) {
        Serial.printf("LED refused: unknown color %s\n", command.color);
        return false;
      }
      // ⚠️ **래치 중에도 받는다.** 눈은 구동 장치가 아니라 상태 출력이다. 다만 표시는
      // 흰색이 이긴다 — 래치가 풀리면 여기 저장한 색이 곧바로 나온다.
      g_eye_commanded = color;
      g_eye_blink_hz = command.blink_hz;
      g_eye_blink_on = true;
      g_eye_blink_toggled_ms = millis();
      pollEyeLed();
      return true;
    }

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
  // ⚠️ **온보드가 스스로 눈을 감은 시간은 호스트 침묵으로 세지 않는다.** 벤더
  // `action_run` 은 구간마다 delay() 로 블로킹한다(실측 1,062ms). 그동안 loop() 가
  // 멈춰 UDP 수신도 타임아웃 검사도 함께 멈추므로, 깨어나면 «300ms 넘게 명령이
  // 없었다» 로 보여 **로봇이 제 낮잠 때문에 래치한다** — 2026-09-15 실기에서 ACTION 1
  // 직후 failsafe_count 9→10. 호스트는 10Hz 송신을 한 번도 끊지 않았고 밀렸던 전문은
  // 10ms 안에 몰려 처리됐다. 침묵한 것은 호스트가 아니라 우리였다.
  //
  // 대안이던 «그냥 래치한다» 는 더 나쁘다 — 액션마다 호스트가 RESET_SAFE 를 보내게
  // 되어 **안전 래치를 자동으로 푸는 습관**을 가르친다. 호스트가 정말 죽었다면 액션이
  // 끝난 뒤 300ms 안에 그대로 래치된다(최악 약 1.3초). 그 사이 동작은 서기·앉기·
  // 엎드리기뿐이라 몸이 이동하지 않는다 — 위험이 유계다.
  g_last_valid_command_ms = uptimeMs();
  Serial.printf("CMD: seq=%lld type=%s applied=%d safe=%d\n",
                static_cast<long long>(decoded.command.seq),
                mechadog::to_string(decoded.command.type), applied, g_motion_state.safe_latched);
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

void pollSensorPerformance(uint64_t now) {
  if (!g_sensors.enabled() || now < g_next_sensor_performance_log_ms) return;
  g_next_sensor_performance_log_ms = now + kSensorStatusLogIntervalMs;
  // This larger copy happens only once per second, independently of the normal
  // SensorSnapshot read on every loop. Each metric is emitted every five seconds.
  mechadog::SensorPerformanceSnapshot performance;
  if (!g_sensors.performance_snapshot(performance)) {
    if (g_sensor_performance_dropped != UINT32_MAX) ++g_sensor_performance_dropped;
    return;
  }
  const mechadog::SensorElapsedDistribution* metrics[] = {&performance.cycle, &performance.wake,
                                                          &performance.imu, &performance.sonar,
                                                          &performance.adc};
  const char* names[] = {"cycle", "wake", "imu", "sonar", "adc"};
  char text[mechadog::kSensorPerformanceLineBytes];
  const int length = mechadog::format_sensor_performance_line(
      text, sizeof(text), performance, *metrics[g_sensor_performance_metric],
      names[g_sensor_performance_metric], MECHADOG_SENSOR_CORE, g_sensor_performance_dropped);
  // p95/p99 are estimated histogram upper bounds, not exact percentiles. These
  // are wall durations, not task CPU percentages. As with Sensor status, this
  // is best-effort capacity guarding; other startup UART writers can contend.
  if (length > 0 && Serial.availableForWrite() >= length + 128) {
    Serial.write(reinterpret_cast<const uint8_t*>(text), static_cast<size_t>(length));
  } else if (g_sensor_performance_dropped != UINT32_MAX) {
    ++g_sensor_performance_dropped;
  }
  g_sensor_performance_metric = (g_sensor_performance_metric + 1) % 5;
}

void pollTelemetry() {
  const uint32_t sensor_now = millis();
  const mechadog::SensorSnapshot sensors = g_sensors.snapshot(sensor_now);
  const uint64_t now = uptimeMs();

  // ── Tier 1 판정 (3.2.2 · 3.2.6) — 발행보다 먼저 한다 ─────────────────
  mechadog::SafetyReading reading;
  reading.now_ms = sensor_now;
  reading.dist_valid = sensors.dist_valid;
  reading.dist_cm = sensors.dist_cm;
  reading.dist_age_ms = sensors.dist_age_ms;
  reading.batt_valid = sensors.batt_valid;
  reading.batt_v = sensors.batt_v;
  reading.batt_age_ms = sensors.batt_age_ms;
  const mechadog::SafetyVerdict safety = g_safety.update(reading);

  if (safety.obstacle_started) {
    // 호스트에게 묻지 않고 즉시 세운다. 표본 나이를 함께 남겨 «감지 → 정지» 를
    // 온보드 시각으로 입증한다 (3.2.6 DoD ①).
    g_motion.stop();
    g_motion_state.stop();
    Serial.printf("Obstacle stop: dist=%.1fcm sample_age=%lums\n", sensors.dist_cm,
                  static_cast<unsigned long>(safety.decision_age_ms));
  }
  if (safety.shutdown) {
    Serial.printf("Battery shutdown: %.2fV <= %.2fV\n", sensors.batt_v,
                  static_cast<double>(g_safety.thresholds().battery_shutdown_v));
    latchFailsafe("battery below shutdown threshold");
  }
  const uint64_t command_age = now - g_last_valid_command_ms;
  mechadog::TelemetrySample sample;
  // Acquisition validity/freshness does not certify body axes or voltage
  // calibration. Sensors may now run with actuators (sensor_hal.h I2C rule);
  // verified walking in the air on 2026-09-12, floor walking still pending.
  sample.sensors_valid = sensors.all_valid();
  // 래치가 가장 위다. 그다음이 온보드 반사 — 호스트는 이 보고로만 회피 시퀀스를
  // 연다 (3.2.6 DoD ②③). 풀리면 호스트가 마지막으로 알려준 상태로 돌아간다.
  sample.state = g_motion_state.safe_latched ? mechadog::FsmState::Failsafe
                 : safety.obstacle           ? mechadog::FsmState::Avoid
                                             : g_reported_state;
  sample.dist_cm = sensors.dist_cm;
  sample.pitch = sensors.pitch;
  sample.roll = sensors.roll;
  sample.yaw = sensors.yaw;
  sample.batt_v = sensors.batt_v;
  sample.last_cmd_age_ms = static_cast<int64_t>(command_age);
  sample.lowbatt = safety.lowbatt;
  // Fall detection is not implemented: false means no onboard tipped-stop has
  // been activated. It does NOT establish a verified upright posture.
  sample.tipped = false;
  sample.link_ok = g_have_valid_command && command_age <= kLinkHealthyAgeMs;
  // 온보드 반사 정지가 지금 걸려 있는가. ⚠️ `state` 의 `AVOID` 로는 해제를 알 수
  // 없다 — 호스트가 되돌려주는 반향과 구분되지 않기 때문이다 ([ADR-22]).
  sample.obstacle = safety.obstacle;
  sample.include_obstacle = true;
  sample.safety_latched = g_motion_state.safe_latched;
  sample.service_mode = serviceModeActive();
  sample.include_service_mode = MECHADOG_SERVICE_MODE != 0;
  // Publisher enforces 100 ms cadence and rejects missing/invalid sensor data.
  g_telemetry.poll(sample);

  if (now >= g_next_sensor_status_log_ms) {
    if (g_next_sensor_status_log_ms == 0) {
      // Start halfway between status emissions, even when setup was slow.
      g_next_sensor_performance_log_ms = now + kSensorStatusLogIntervalMs / 2;
    }
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
  pollSensorPerformance(now);
}

}  // namespace

namespace mechadog {
// OTA handlers ask these before accepting a write or a confirmation reboot.
// Actuator-OFF builds are always parked; actuator builds park via SERVICE
// mode (writes) or any engaged safe latch (confirm after the update reboot —
// pending-verify images refuse RESET_SAFE below, so latched means stationary).
bool serviceModeParked() {
#if MECHADOG_ENABLE_ACTUATORS
  return serviceModeActive();
#else
  return true;
#endif
}
bool otaParkedForReboot() {
#if MECHADOG_ENABLE_ACTUATORS
  return serviceModeActive() || g_motion_state.safe_latched;
#else
  return true;
#endif
}
}  // namespace mechadog

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
  if (!mechadog::beginStationaryOta()) {
    Serial.println("OTA initialization failed; restarting for bootloader recovery");
    ESP.restart();
  }
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
#if MECHADOG_ENABLE_TASK_WDT && !MECHADOG_ENABLE_ACTUATORS
  // setup() runs inside loopTask. Monitor progress without waiting for DHCP.
  // Errors are fatal in this actuator-OFF build, never silently unprotected.
  ESP_ERROR_CHECK(mechadog::startTaskWatchdog());
  Serial.printf("Loop watchdog: deadline_ms=%lu poll_ms=%lu SDK_WDT=unchanged reset_reason=%d\n",
                static_cast<unsigned long>(mechadog::kLoopWatchdogDeadlineUs / 1000),
                static_cast<unsigned long>(mechadog::kLoopWatchdogPollMs),
                static_cast<int>(esp_reset_reason()));
#elif MECHADOG_SERVICE_MODE
  // Actuator build: the watchdog stays disarmed until SERVICE mode parks the
  // body. Arming at boot would allow a restart while the legs are powered.
  pinMode(kServiceButtonPin, INPUT_PULLUP);
  Serial.printf("Service mode available: button=GPIO%u command=SERVICE mode=enter|exit\n",
                static_cast<unsigned>(kServiceButtonPin));
#endif
}

void loop() {
#if MECHADOG_WATCHDOG_FAULT_PROBE
  // Explicit bench-only UART fault injection. Never in default/release builds.
  // No startup hang and no network trigger; a fresh H is required after boot.
  if (Serial.available() && Serial.read() == 'H') {
    Serial.println("WDT_FAULT_PROBE H accepted; feed then intentional loop hang");
    Serial.flush();
    ESP_ERROR_CHECK(mechadog::feedTaskWatchdog());
    for (;;) {
      asm volatile("nop");
    }
  }
#endif
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
  if (g_have_valid_command && !g_motion_state.safe_latched &&
      watchdog_now - g_last_valid_command_ms >= kCommandTimeoutMs) {
    latchFailsafe("command timeout >= 300 ms");
  }

  // Safety decisions precede acquisition snapshot and telemetry publication.
  // 눈 LED 도 그 뒤다 — 래치가 걸린 뒤의 흰색이 같은 회전에서 나가야 한다.
  pollEyeLed();
  pollTelemetry();
  pollWifiDiagnostics();
#if MECHADOG_ENABLE_OTA
  mechadog::pollStationaryOta(WiFi.status() == WL_CONNECTED, g_sensors.snapshot(millis()));
#endif
#if MECHADOG_SERVICE_MODE
  pollServiceButton();
#endif
#if MECHADOG_ENABLE_TASK_WDT
  // Runtime-armed builds feed only while armed; boot-armed builds are always
  // armed so this is identical to the previous unconditional feed for them.
  if (mechadog::taskWatchdogArmed()) {
    ESP_ERROR_CHECK(mechadog::feedTaskWatchdog());
  }
#endif
  delay(1);
}
