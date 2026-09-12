// WBS 3.2.4: independent loop progress monitor; SDK TWDT/IWDT are untouched.
#ifndef MECHADOG_TASK_WATCHDOG_H
#define MECHADOG_TASK_WATCHDOG_H
#include <esp_err.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
namespace mechadog {
// Design budget, not a reset guarantee. Scheduling/restart require real tests.
constexpr int64_t kLoopWatchdogDeadlineUs = 750000;
constexpr uint32_t kLoopWatchdogPollMs = 10;
constexpr uint32_t kLoopWatchdogStackBytes = 3072;
namespace watchdog_detail {
struct Progress {
  int64_t last_us = 0;
  bool armed = false;
  bool fault = false;
  bool expired(int64_t now) {
    if (armed && (now < last_us || now - last_us >= kLoopWatchdogDeadlineUs)) fault = true;
    return fault;
  }
  bool feed(int64_t now) {
    // A late iteration must not erase a missed deadline.
    if (!armed || expired(now)) return false;
    last_us = now;
    return true;
  }
};
struct Runtime {
  portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
  Progress progress;
  TaskHandle_t owner = nullptr;
  TaskHandle_t monitor = nullptr;
};
inline Runtime& runtime() {
  static Runtime value;
  return value;
}
inline bool overdue() {
  Runtime& state = runtime();
  portENTER_CRITICAL(&state.lock);
  const bool result = state.progress.expired(esp_timer_get_time());
  portEXIT_CRITICAL(&state.lock);
  return result;
}
inline void monitorTask(void*) {
  // Only the observer blocks. No I/O, bus accesses or replacement feeds.
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(kLoopWatchdogPollMs));
    if (overdue()) esp_restart();
  }
}
}  // namespace watchdog_detail
inline esp_err_t startTaskWatchdog() {
  static_assert(pdMS_TO_TICKS(kLoopWatchdogPollMs) > 0, "Watchdog poll must block for a tick");
  watchdog_detail::Runtime& state = watchdog_detail::runtime();
  const TaskHandle_t owner = xTaskGetCurrentTaskHandle();
  if (owner == nullptr) return ESP_ERR_INVALID_STATE;
  portENTER_CRITICAL(&state.lock);
  if (state.progress.armed) {
    portEXIT_CRITICAL(&state.lock);
    return ESP_ERR_INVALID_STATE;
  }
  state.owner = owner;
  state.progress.last_us = esp_timer_get_time();
  state.progress.fault = false;
  state.progress.armed = true;
  portEXIT_CRITICAL(&state.lock);
  // Same core as the loop; above ordinary application tasks, below SDK IPC.
  // One startup allocation (stack + TCB); allocation failure is fatal to caller.
  const BaseType_t result =
      xTaskCreatePinnedToCore(watchdog_detail::monitorTask, "loop_monitor", kLoopWatchdogStackBytes,
                              nullptr, configMAX_PRIORITIES - 2, &state.monitor, xPortGetCoreID());
  if (result != pdPASS) {
    portENTER_CRITICAL(&state.lock);
    state.progress.armed = false;
    state.owner = nullptr;
    portEXIT_CRITICAL(&state.lock);
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}
inline esp_err_t feedTaskWatchdog() {
  watchdog_detail::Runtime& state = watchdog_detail::runtime();
  const TaskHandle_t caller = xTaskGetCurrentTaskHandle();
  portENTER_CRITICAL(&state.lock);
  esp_err_t result = ESP_ERR_INVALID_STATE;
  if (state.progress.armed && caller == state.owner) {
    result = state.progress.feed(esp_timer_get_time()) ? ESP_OK : ESP_ERR_TIMEOUT;
  }
  portEXIT_CRITICAL(&state.lock);
  return result;
}
inline bool taskWatchdogArmed() {
  watchdog_detail::Runtime& state = watchdog_detail::runtime();
  portENTER_CRITICAL(&state.lock);
  const bool armed = state.progress.armed;
  portEXIT_CRITICAL(&state.lock);
  return armed;
}
}  // namespace mechadog
#endif
