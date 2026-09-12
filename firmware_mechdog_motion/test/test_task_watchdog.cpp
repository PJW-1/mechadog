// SDK call contract/error paths; this cannot measure real watchdog reset latency.
#include <stdio.h>
#include <stdlib.h>

#include <string>

#include "task_watchdog.h"

// Do not use assert(): some optimizing toolchains define NDEBUG by default.
void check(bool condition) {
  if (!condition) {
    fputs("Task WDT SDK contract check failed\n", stderr);
    abort();
  }
}

namespace {
std::string calls;
esp_err_t init_result = ESP_OK;
esp_err_t status_result = ESP_ERR_NOT_FOUND;
esp_err_t add_result = ESP_OK;
esp_err_t reset_result = ESP_OK;
constexpr esp_err_t kInjectedError = 99;

void clear() {
  calls.clear();
  init_result = ESP_OK;
  status_result = ESP_ERR_NOT_FOUND;
  add_result = ESP_OK;
  reset_result = ESP_OK;
}
}  // namespace

esp_err_t esp_task_wdt_init(uint32_t seconds, bool panic) {
  check(seconds == 1 && panic);
  calls += 'I';
  return init_result;
}
esp_err_t esp_task_wdt_status(TaskHandle_t task) {
  check(task == nullptr);
  calls += 'S';
  return status_result;
}
esp_err_t esp_task_wdt_add(TaskHandle_t task) {
  check(task == nullptr);
  calls += 'A';
  return add_result;
}
esp_err_t esp_task_wdt_reset() {
  calls += 'R';
  return reset_result;
}

int main() {
  clear();
  check(mechadog::startTaskWatchdog() == ESP_OK && calls == "ISAR");
  clear();
  status_result = ESP_OK;
  check(mechadog::startTaskWatchdog() == ESP_OK && calls == "ISR");
  clear();
  init_result = kInjectedError;
  check(mechadog::startTaskWatchdog() == kInjectedError && calls == "I");
  clear();
  status_result = kInjectedError;
  check(mechadog::startTaskWatchdog() == kInjectedError && calls == "IS");
  clear();
  add_result = kInjectedError;
  check(mechadog::startTaskWatchdog() == kInjectedError && calls == "ISA");
  clear();
  reset_result = kInjectedError;
  check(mechadog::startTaskWatchdog() == kInjectedError && calls == "ISAR");
  clear();
  check(mechadog::feedTaskWatchdog() == ESP_OK && calls == "R");
  clear();
  reset_result = kInjectedError;
  check(mechadog::feedTaskWatchdog() == kInjectedError && calls == "R");
  puts("Task WDT SDK contract: 8 cases passed; hardware timing unverified");
}
