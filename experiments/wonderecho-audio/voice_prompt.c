#include "voice_prompt.h"
#include "audio_play_api.h"
#include "ci_flash_data_info.h"
#include "sdk_default_config.h"

static volatile int result;
static TaskHandle_t receiver;
static uint32_t notification;

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
    audio_play_set_vol_gain(40);
    return play_prompt(address, 1, prompt_done) == 0 ? 0 : -1;
}

int we_prompt_result(void) { return result; }
void we_prompt_cancel(void) { stop_play(NULL, NULL); }
