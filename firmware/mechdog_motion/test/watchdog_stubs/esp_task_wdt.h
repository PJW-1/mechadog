// A regression to SDK TWDT manipulation must fail the host build.
#pragma once
#include "esp_err.h"
#include "freertos/task.h"
esp_err_t esp_task_wdt_init(uint32_t, bool) = delete;
esp_err_t esp_task_wdt_add(TaskHandle_t) = delete;
esp_err_t esp_task_wdt_delete(TaskHandle_t) = delete;
esp_err_t esp_task_wdt_deinit() = delete;
esp_err_t esp_task_wdt_reset() = delete;
