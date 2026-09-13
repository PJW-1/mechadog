#include "voice_prompt.h"
#include "audio_play_api.h"
#include "ci_flash_data_info.h"
#include "sdk_default_config.h"
#include "ci130x_iisdma.h"

static volatile int result;
static TaskHandle_t receiver;
static uint32_t notification;
volatile uint32_t we_play_trace, we_read_trace;
volatile uint32_t we_output_irqs, we_buffer_events;
volatile uint32_t we_hw_starts, we_decoded_frames, we_wave_bytes;
volatile uint32_t we_config_result, we_start_result;

static void prompt_done(int32_t state)
{
    if (state == AUDIO_PLAY_CB_STATE_PLAY_THRESHOLD) return;
    result = state == AUDIO_PLAY_CB_STATE_DONE ? 1 : -1;
    if (receiver) xTaskNotify(receiver, notification, eSetBits);
}

int we_prompt_start(TaskHandle_t worker, uint32_t event)
{
    uint32_t address = 0, size = 0;
    if (get_audio_play_state() != AUDIO_PLAY_STATE_IDLE ||
        get_userfile_addr(65000, &address, &size) != 0 || size < 48 || size > 128000)
        return -1;
    receiver = worker; notification = event; result = 0;
    we_output_irqs = 0; we_buffer_events = 0;
    we_hw_starts = we_decoded_frames = we_wave_bytes = 0;
    we_config_result = we_start_result = UINT32_MAX;
    audio_play_set_vol_gain(40);
    return play_prompt(address, 1, prompt_done) == 0 ? 0 : -1;
}

int we_prompt_result(void) { return result; }
void we_prompt_cancel(void) { stop_play(NULL, NULL); }
void we_prompt_snapshot(uint32_t values[16])
{
    values[0] = we_play_trace; values[1] = we_read_trace;
    values[2] = get_audio_play_state(); values[3] = (uint32_t)result;
    values[4] = xPortGetFreeHeapSize(); values[5] = xPortGetMinimumEverFreeHeapSize();
    values[6] = 214; values[7] = configTICK_RATE_HZ;
    values[8] = we_output_irqs; values[9] = we_buffer_events;
    values[10] = we_hw_starts; values[11] = we_decoded_frames;
    values[12] = we_wave_bytes; values[13] = IISDMA0->IISDMACTRL;
    values[14] = we_config_result; values[15] = we_start_result;
}
