#pragma once
#include <stdint.h>
using BaseType_t = int;
using UBaseType_t = unsigned;
using TickType_t = unsigned;
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define pdMS_TO_TICKS(ms) (ms)
constexpr BaseType_t pdPASS = 1;
constexpr unsigned configMAX_PRIORITIES = 25;
void testEnterCritical(portMUX_TYPE*);
void testExitCritical(portMUX_TYPE*);
#define portENTER_CRITICAL(lock) testEnterCritical(lock)
#define portEXIT_CRITICAL(lock) testExitCritical(lock)
