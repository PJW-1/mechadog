// Standalone WBS 3.2.4 bench probe. No motion, I2C, Wi-Fi or vendor library.
// UART 'H' deliberately hangs this sketch; reboot requires a fresh 'H' to repeat.
#include <Arduino.h>
#include <esp_system.h>

#include "task_watchdog.h"  // Add the motion src directory to the compile include path.

void setup() {
  Serial.begin(115200);
  Serial.printf("WDT probe boot: reset_reason=%d\n", static_cast<int>(esp_reset_reason()));
  Serial.println("Receive-only until H is sent. H deliberately hangs loopTask once.");
  ESP_ERROR_CHECK(mechadog::startTaskWatchdog());
  Serial.println("Loop watchdog probe: deadline_ms=750 poll_ms=10 SDK_WDT=unchanged");
}

void loop() {
  if (Serial.available() && Serial.read() == 'H') {
    // Flush the marker BEFORE the last feed; no UART logging inside the hang.
    // UART host timing is approximate; use reset edges for the strict 1 s DoD.
    Serial.println("WDT probe: H accepted; next operation is last feed then intentional hang");
    Serial.flush();
    ESP_ERROR_CHECK(mechadog::feedTaskWatchdog());
    for (;;) {
      asm volatile("nop");  // Test-only fault injection, never compiled into the motion app.
    }
  }
  ESP_ERROR_CHECK(mechadog::feedTaskWatchdog());
  delay(1);
}
