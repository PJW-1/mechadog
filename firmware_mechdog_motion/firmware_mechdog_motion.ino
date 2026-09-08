#include <WiFi.h>
#include <WiFiUdp.h>

#include "src/command_parser.h"
#include "src/motion_hal.h"

namespace {

constexpr uint16_t kCommandPort = 5001;
constexpr uint32_t kCommandTimeoutMs = 300;
constexpr uint32_t kReconnectIntervalMs = 3000;
constexpr size_t kPacketBufferSize = 512;

WiFiUDP g_udp;
mechadog::CommandParser g_parser;
mechadog::MotionHal g_motion;

char g_packet[kPacketBufferSize + 1];
bool g_safe_latched = true;
bool g_have_valid_command = false;
uint32_t g_last_valid_command_ms = 0;
uint32_t g_last_reconnect_attempt_ms = 0;
uint32_t g_failsafe_count = 0;
bool g_udp_started = false;

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
      Serial.println("SAFE latch cleared; waiting for a new MOVE");
      return true;

    case mechadog::CmdType::Move:
      if (g_safe_latched) return false;
      g_motion.move(command.step, command.angle);
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
  g_last_valid_command_ms = millis();
  const bool applied = applyCommand(decoded.command);
  Serial.printf("CMD: seq=%lld type=%s applied=%d safe=%d\n",
                static_cast<long long>(decoded.command.seq),
                mechadog::to_string(decoded.command.type), applied, g_safe_latched);
  sendAck(decoded, applied);
}

void connectSavedWifi() {
  WiFi.mode(WIFI_STA);
  // Command latency matters more than power saving on the body MCU. Modem sleep
  // can delay UDP bursts long enough to trip the 300 ms motion watchdog.
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);
  WiFi.begin();  // Reuse the router credentials already saved in ESP32 NVS.

  Serial.print("Connecting to saved Wi-Fi");
  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 20000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
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

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("MechDog command receiver booting");

  g_motion.begin();
  g_motion.stop();
  connectSavedWifi();

  if (WiFi.status() == WL_CONNECTED) {
    startUdpIfNeeded();
    Serial.printf("Wi-Fi connected: %s RSSI=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    Serial.printf("Actuators: %s, initial SAFE latch: ON\n",
                  g_motion.actuators_enabled() ? "ON" : "OFF");
  } else {
    Serial.println("Wi-Fi connection failed; SAFE latch remains ON");
  }
}

void loop() {
  const uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    // WiFiUDP의 로컬 소켓은 인터페이스가 끊긴 뒤에도 started 상태로 남을 수
    // 있다. 명시적으로 닫아야 재접속 뒤 같은 포트에 다시 bind한다.
    if (g_udp_started) {
      g_udp.stop();
      g_udp_started = false;
      Serial.println("UDP stopped; waiting for Wi-Fi reconnect");
    }
    latchFailsafe("Wi-Fi disconnected");
    if (now - g_last_reconnect_attempt_ms >= kReconnectIntervalMs) {
      g_last_reconnect_attempt_ms = now;
      WiFi.reconnect();
    }
  } else {
    startUdpIfNeeded();
    const int packet_size = g_udp.parsePacket();
    if (packet_size > 0) handlePacket(packet_size);
  }

  // Read time again after packet handling. A command can record millis() one
  // tick newer than `now`; subtracting that from the older unsigned value wraps.
  const uint32_t watchdog_now = millis();
  if (g_have_valid_command && !g_safe_latched &&
      watchdog_now - g_last_valid_command_ms >= kCommandTimeoutMs) {
    latchFailsafe("command timeout >= 300 ms");
  }

  delay(1);
}
