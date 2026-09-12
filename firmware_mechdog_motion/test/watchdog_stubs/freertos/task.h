#pragma once
#include "FreeRTOS.h"
using TaskHandle_t = void*;
using TaskFunction_t = void (*)(void*);
TaskHandle_t xTaskGetCurrentTaskHandle();
BaseType_t xPortGetCoreID();
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t, const char*, uint32_t, void*, UBaseType_t,
                                   TaskHandle_t*, BaseType_t);
void vTaskDelay(TickType_t);
