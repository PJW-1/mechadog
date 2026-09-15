#include <WiFi.h>

namespace {
constexpr char kTestSsid[] = "HW_ESP32S3CAM";
constexpr unsigned long kConnectTimeoutMs = 15000;
constexpr unsigned long kReportIntervalMs = 2000;
unsigned long last_report_ms = 0;
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);

  // This diagnostic intentionally does not initialize MechDog motors or servos.
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(kTestSsid);

  Serial.printf("WIFI_STA_CONNECTING ssid=\"%s\"\n", kTestSsid);
  const unsigned long started_ms = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started_ms < kConnectTimeoutMs) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WIFI_STA_FAILED status=%d\n", WiFi.status());
    return;
  }

  Serial.println("WIFI_STA_CONNECTED");
  Serial.printf("SSID=%s\n", WiFi.SSID().c_str());
  Serial.printf("IP=%s\n", WiFi.localIP().toString().c_str());
  Serial.printf("GATEWAY=%s\n", WiFi.gatewayIP().toString().c_str());
  Serial.printf("RSSI=%d\n", WiFi.RSSI());
}

void loop() {
  if (millis() - last_report_ms >= kReportIntervalMs) {
    Serial.printf("WIFI_STA_STATUS=%d RSSI=%d\n", WiFi.status(), WiFi.RSSI());
    last_report_ms = millis();
  }
  delay(20);
}
