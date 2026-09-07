#include <WiFi.h>

namespace {
constexpr unsigned long kScanIntervalMs = 5000;
unsigned long last_scan_ms = 0;

void scanNetworks() {
  Serial.println();
  Serial.println("WIFI_SCAN_BEGIN");

  const int count = WiFi.scanNetworks(/*async=*/false, /*show_hidden=*/true);
  if (count < 0) {
    Serial.printf("WIFI_SCAN_ERROR code=%d\n", count);
    return;
  }

  Serial.printf("WIFI_SCAN_COUNT=%d\n", count);
  for (int index = 0; index < count; ++index) {
    Serial.printf(
        "WIFI_AP index=%d ssid=\"%s\" rssi=%d channel=%d encryption=%d\n",
        index + 1,
        WiFi.SSID(index).c_str(),
        WiFi.RSSI(index),
        WiFi.channel(index),
        static_cast<int>(WiFi.encryptionType(index)));
  }
  WiFi.scanDelete();
  Serial.println("WIFI_SCAN_END");
}
}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);

  // This diagnostic intentionally does not initialize MechDog motors or servos.
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);

  Serial.println("MECHDOG_WIFI_DIAGNOSTIC_READY");
  Serial.printf("CHIP_MODEL=%s\n", ESP.getChipModel());
  Serial.printf("CHIP_REVISION=%d\n", ESP.getChipRevision());
  Serial.printf("MAC=%s\n", WiFi.macAddress().c_str());

  scanNetworks();
  last_scan_ms = millis();
}

void loop() {
  if (millis() - last_scan_ms >= kScanIntervalMs) {
    scanNetworks();
    last_scan_ms = millis();
  }
  delay(20);
}
