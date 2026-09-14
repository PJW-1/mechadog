#include "voice_prompt.h"
#include "audio_play_api.h"
#include "ci_flash_data_info.h"
#include "sdk_default_config.h"
#include "ci130x_iisdma.h"
#include "ci130x_gpio.h"
#include "ci130x_system.h"
#include "ci130x_iic.h"
#include "ci130x_dpmu.h"
#include "ci130x_scu.h"
#include "ci130x_pdm.h"
#include "board.h"   /* power_amplifier_on() */

extern int g_pa_pin_valid_level; /* CI-D02GS01J.c auto-detected PA enable polarity. */

/* v20: replay the prompt WE_PASS_COUNT times, toggling the PC4 amp-enable
   level each pass, so the amplifier's enable polarity can be identified by
   ear (and via the loopback WAV): passes 0/2/4 drive PC4 low, 1/3/5 high. */
#define WE_PASS_COUNT 1   /* v21: sweep off - one variable at a time */
static volatile int we_pass;
static uint32_t we_addr, we_size;

static volatile int result;
static TaskHandle_t receiver;
static uint32_t notification;
volatile uint32_t we_play_trace, we_read_trace;
volatile uint32_t we_output_irqs, we_buffer_events;
volatile uint32_t we_hw_starts, we_decoded_frames, we_wave_bytes;
volatile uint32_t we_config_result, we_start_result;
volatile uint32_t we_tx_peak;

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
    we_tx_peak = 0;
    we_addr = address; we_size = size; we_pass = 0;
    /* The physical HPOUT driver may live in the PDM block (pdm_hpout_mute);
       power it and unmute once — harmless if the CODEC owns the pad. */
    scu_set_device_gate((uint32_t)PDM, ENABLE);
    scu_set_device_reset((uint32_t)PDM);
    scu_set_device_reset_release((uint32_t)PDM);
    pdm_power_up(PDM_CURRENT_128I);
    pdm_hpout_mute_disable();
    /* Enable the amplifier and let it settle BEFORE the first sample goes out.
       The SDK player enables the PA at the same instant playback starts, so the
       only cushion was the 250 ms of silence baked into the prompt - and the
       loopback capture shows that is not enough. Measured against the source
       WAV on the same timeline: speech begins at 0.250 s in the file but does
       not reach the speaker until 0.400 s, so the first ~150 ms is lost and
       "신원" is heard as "인원". The vendor SDK waits 300 ms elsewhere
       (vTaskDelay(300); //等待功放开启); 400 ms covers what we measured.
       Waiting here costs no flash, unlike lengthening the lead silence - the
       prompt already uses 48,000 of the 53,248 bytes free in the user area. */
    gpio_set_output_low_level(PC, pin_4);
    vTaskDelay(pdMS_TO_TICKS(400));
    audio_play_set_vol_gain(40);
    return play_prompt(address, 1, prompt_done) == 0 ? 0 : -1;
}

/* Called when a pass finishes: start the next pass with the opposite PC4
   level. Returns 1 while passes remain, 0 when the sweep is done. */
int we_prompt_advance(void)
{
    we_pass++;
    if (we_pass >= WE_PASS_COUNT) return 0;
    if (we_pass & 1) gpio_set_output_high_level(PC, pin_4);
    else             gpio_set_output_low_level(PC, pin_4);
    result = 0;
    return play_prompt(we_addr, 1, prompt_done) == 0;
}

int we_prompt_result(void) { return result; }
void we_prompt_cancel(void) { stop_play(NULL, NULL); }
void we_prompt_snapshot(uint32_t values[16])
{
    values[0] = we_play_trace; values[1] = we_read_trace;
    values[2] = get_audio_play_state(); values[3] = (uint32_t)result;
    values[4] = xPortGetFreeHeapSize(); values[5] = xPortGetMinimumEverFreeHeapSize();
    values[6] = 238; values[7] = configTICK_RATE_HZ;
    values[8] = we_output_irqs; values[9] = we_buffer_events;
    values[10] = we_hw_starts; values[11] = we_decoded_frames;
    values[12] = we_wave_bytes; values[13] = IISDMA0->IISDMACTRL;
    values[14] = we_config_result; values[15] = we_start_result;
}

/* The analog chain is verified; the register dump that used to live here is
   gone. Reading the codec's reserved range during playback killed the analog
   output once - never read undocumented registers on this part. */
void we_prompt_hw_snapshot(uint32_t values[16], uint32_t tag)
{
    for (unsigned i = 0; i < 15; ++i) values[i] = 0;
    values[15] = tag;
}
