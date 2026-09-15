// Host simulation of production state/adapter code; NOT hardware timing.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "task_watchdog.h"
namespace {
int owner_tag, other_tag, monitor_tag;
int64_t clock_us = 0;
TaskHandle_t current = &owner_tag;
BaseType_t create_result = pdPASS;
TaskFunction_t task_fn = nullptr;
int critical_depth = 0, checks = 0, delays = 0, restarts = 0;
struct Restart {};
struct StopMonitor {};
void check(bool condition) {
  ++checks;
  if (!condition) {
    fprintf(stderr, "Loop watchdog check %d failed\n", checks);
    abort();
  }
}
void clear() {
  auto& state = mechadog::watchdog_detail::runtime();
  state.progress = mechadog::watchdog_detail::Progress{};
  state.owner = nullptr;
  state.monitor = nullptr;
  clock_us = 0;
  current = &owner_tag;
  create_result = pdPASS;
  task_fn = nullptr;
  delays = 0;
  restarts = 0;
  check(critical_depth == 0);
}
}  // namespace
void testEnterCritical(portMUX_TYPE*) {
  check(critical_depth++ == 0);
}
void testExitCritical(portMUX_TYPE*) {
  check(--critical_depth == 0);
}
int64_t esp_timer_get_time() {
  check(critical_depth == 1);
  return clock_us;
}
TaskHandle_t xTaskGetCurrentTaskHandle() {
  return current;
}
BaseType_t xPortGetCoreID() {
  return 1;
}
// Exact FreeRTOS API signature: changing void* to const void* would change the mock overload.
// cppcheck-suppress constParameterPointer
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t fn, const char* name, uint32_t stack, void* arg,
                                   UBaseType_t priority, TaskHandle_t* handle, BaseType_t core) {
  check(critical_depth == 0 && strcmp(name, "loop_monitor") == 0);
  check(stack == 3072 && arg == nullptr && priority == 23 && core == 1);
  if (create_result == pdPASS) {
    task_fn = fn;
    *handle = &monitor_tag;
  }
  return create_result;
}
void vTaskDelay(TickType_t ticks) {
  check(critical_depth == 0 && ticks == 10);
  clock_us += 10000;
  if (++delays > 76) throw StopMonitor{};
}
void esp_restart() {
  check(critical_depth == 0);
  ++restarts;
  throw Restart{};
}
int main() {
  using mechadog::feedTaskWatchdog;
  using mechadog::startTaskWatchdog;
  using mechadog::watchdog_detail::overdue;
  clear();
  check(!mechadog::taskWatchdogArmed());
  check(feedTaskWatchdog() == ESP_ERR_INVALID_STATE && !overdue());
  current = nullptr;
  check(startTaskWatchdog() == ESP_ERR_INVALID_STATE);
  clear();
  create_result = 0;
  check(startTaskWatchdog() == ESP_ERR_NO_MEM && !overdue());
  check(!mechadog::taskWatchdogArmed());
  check(feedTaskWatchdog() == ESP_ERR_INVALID_STATE);
  create_result = pdPASS;
  check(startTaskWatchdog() == ESP_OK);
  check(mechadog::taskWatchdogArmed());
  check(startTaskWatchdog() == ESP_ERR_INVALID_STATE);
  current = &other_tag;
  clock_us = 700000;
  check(feedTaskWatchdog() == ESP_ERR_INVALID_STATE);
  current = &owner_tag;
  clock_us = 749999;
  check(!overdue());
  clock_us = 750000;
  check(overdue() && feedTaskWatchdog() == ESP_ERR_TIMEOUT && overdue());
  clear();
  check(startTaskWatchdog() == ESP_OK);
  clock_us = 750001;
  check(feedTaskWatchdog() == ESP_ERR_TIMEOUT);  // Late feed before observer also latches.
  clock_us = 1;
  check(overdue());
  clear();
  clock_us = 9000000000LL;
  check(startTaskWatchdog() == ESP_OK);
  for (int i = 0; i < 10000; ++i) {
    clock_us += 50000;
    check(feedTaskWatchdog() == ESP_OK && !overdue());
  }
  --clock_us;
  check(overdue());  // Monotonic clock reversal fails closed.
  clear();
  check(startTaskWatchdog() == ESP_OK);
  clock_us = 749999;
  check(feedTaskWatchdog() == ESP_OK);
  clock_us += 749999;
  check(!overdue());
  ++clock_us;
  check(overdue());
  clear();
  check(startTaskWatchdog() == ESP_OK && task_fn != nullptr);
  current = &monitor_tag;
  try {
    task_fn(nullptr);  // Real observer loop with simulated clock.
    check(false);
  } catch (const Restart&) {
    check(clock_us == 750000 && restarts == 1 && delays == 75);
  } catch (const StopMonitor&) {
    check(false);  // Observer must not feed its owner on behalf.
  }
  check(critical_depth == 0);
  printf("Loop watchdog: %d assertions passed; hardware reset latency UNVERIFIED\n", checks);
}
