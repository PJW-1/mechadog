// WBS 3.2.4: Arduino-ESP32 2.0.12 / ESP-IDF 4.4 task watchdog.
#ifndef MECHADOG_TASK_WATCHDOG_H
#define MECHADOG_TASK_WATCHDOG_H

#include <esp_task_wdt.h>

namespace mechadog {

// IDF 4.4 accepts whole seconds. A 1 s timeout is NOT evidence that reset
// completes within 1 s: shared task feeds, interrupt and panic latency matter.
constexpr uint32_t kTaskWatchdogTimeoutSeconds = 1;

inline esp_err_t startTaskWatchdog() {
  // Reconfigure the existing TWDT without removing the SDK's idle subscribers.
  // Call only from the task to monitor, after its startup work has completed.
  esp_err_t result = esp_task_wdt_init(kTaskWatchdogTimeoutSeconds, true);
  if (result != ESP_OK) return result;
  result = esp_task_wdt_status(nullptr);
  if (result == ESP_ERR_NOT_FOUND) {
    result = esp_task_wdt_add(nullptr);
  }
  if (result != ESP_OK) return result;
  return esp_task_wdt_reset();
}

inline esp_err_t feedTaskWatchdog() {
  // Must be called by the monitored task only after completing its work.
  // Never feed from a timer, Wi-Fi callback or another task on its behalf.
  return esp_task_wdt_reset();
}

}  // namespace mechadog
#endif
