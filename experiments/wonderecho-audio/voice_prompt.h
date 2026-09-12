#ifndef WE_VOICE_PROMPT_H
#define WE_VOICE_PROMPT_H
#include "FreeRTOS.h"
#include "task.h"
int we_prompt_start(TaskHandle_t worker, uint32_t event);
int we_prompt_result(void); /* 0 pending, 1 finished, -1 failed */
void we_prompt_cancel(void);
#endif
