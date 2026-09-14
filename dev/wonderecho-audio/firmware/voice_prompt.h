#ifndef WE_VOICE_PROMPT_H
#define WE_VOICE_PROMPT_H
#include "FreeRTOS.h"
#include "task.h"
int we_prompt_start(TaskHandle_t worker, uint32_t event);
int we_prompt_result(void); /* 0 pending, 1 finished, -1 failed */
int we_prompt_advance(void); /* start next polarity pass; 1 while passes remain */
void we_prompt_cancel(void);
extern volatile uint32_t we_play_trace, we_read_trace;
extern volatile uint32_t we_output_irqs, we_buffer_events;
extern volatile uint32_t we_hw_starts, we_decoded_frames, we_wave_bytes;
extern volatile uint32_t we_config_result, we_start_result;
void we_prompt_snapshot(uint32_t values[16]);
void we_prompt_hw_snapshot(uint32_t values[16], uint32_t tag);
#endif
