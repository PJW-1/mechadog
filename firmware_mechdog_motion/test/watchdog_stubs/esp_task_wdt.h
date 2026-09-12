// Host test declarations only. Never add this directory to an ESP32 build.
#ifndef MECHADOG_TEST_ESP_TASK_WDT_H
#define MECHADOG_TEST_ESP_TASK_WDT_H
#include <stdint.h>
using esp_err_t = int;
struct TaskControlBlock;
using TaskHandle_t = TaskControlBlock*;
constexpr esp_err_t ESP_OK = 0;
constexpr esp_err_t ESP_ERR_NOT_FOUND = 1;
esp_err_t esp_task_wdt_init(uint32_t seconds, bool panic);
esp_err_t esp_task_wdt_status(TaskHandle_t task);
esp_err_t esp_task_wdt_add(TaskHandle_t task);
esp_err_t esp_task_wdt_reset();
#endif
