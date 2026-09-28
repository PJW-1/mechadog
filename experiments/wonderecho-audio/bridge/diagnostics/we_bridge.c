#include "we_bridge.h"
#include "we_bridge_protocol.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "sdk_default_config.h"
#include "ci130x_uart.h"
#include "ci130x_core_eclic.h"
#include "command_info.h"
#include "prompt_player.h"
#include "audio_play_api.h"
#include "ci_log.h"
#include "system_msg_deal.h"
#include <string.h>

#if MSG_COM_USE_UART_EN || MSG_USE_I2C_EN
#error "WonderEcho adapter owns UART1; disable the unrelated SDK UART/IIC protocols"
#endif
#if CONFIG_CI_LOG_UART == HAL_UART1_BASE
#error "UART1 is reserved for the WonderEcho bridge"
#endif

enum { BRIDGE_TX = 1, BRIDGE_PLAY = 2 };
typedef struct { uint8_t kind; we_bridge_frame frame; TickType_t arrived; } bridge_event;
static QueueHandle_t bridge_events;
static we_bridge_parser parser;
static volatile int ready;
/* RAM-only observations; phases 0:not called, 1:map, 2:queue, 3:UART, 4:ready. */
static volatile unsigned init_phase;
static volatile int init_result = 100; /* 100 means pending, not success. */
static volatile uint32_t rx_bytes, busy_rejected, expired, asr_seen, last_asr;
/* No codec/register dump: only software event counters. */
static volatile uint32_t rx_frames, tx_frames, dropped, unsupported, uart_errors;
static volatile uint32_t play_accepted, play_callbacks, play_failed;

static void bridge_irq(void)
{
    BaseType_t wake = pdFALSE;
    for (unsigned i = 0; i < 32 && !UART_FLAGSTAT(UART1, UART_RXFE); ++i) {
        uint32_t raw = UART1->UARTRdDR;
        ++rx_bytes;
        TickType_t now = xTaskGetTickCountFromISR();
        if (raw & 0xf00u) { ++uart_errors; parser.used = 0; continue; }
        bridge_event event = {0};
        if (we_bridge_feed(&parser, (uint8_t)raw, (uint32_t)now * portTICK_PERIOD_MS, &event.frame)) {
            ++rx_frames; event.kind = BRIDGE_PLAY; event.arrived = now;
            if (xQueueSendFromISR(bridge_events, &event, &wake) != pdPASS) ++dropped;
        }
    }
    UART_IntClear(UART1, UART_RXInt);
    UART_IntClear(UART1, UART_RXTimeoutInt);
    portYIELD_FROM_ISR(wake);
}

static int bridge_send(we_bridge_frame frame)
{
    uint8_t bytes[5];
    we_bridge_encode(frame, bytes);
    TickType_t start = xTaskGetTickCount();
    for (unsigned i = 0; i < sizeof(bytes); ++i) {
        while (UART_FLAGSTAT(UART1, UART_TXFF)) {
            if (xTaskGetTickCount() - start >= pdMS_TO_TICKS(20)) return -1;
            vTaskDelay(1); /* bounded wait for actual FIFO space */
        }
        UART_TXDATAConfig(UART1, bytes[i]);
    }
    return 0;
}

static void bridge_play_done(cmd_handle_t handle)
{
    (void)handle;
    ++play_callbacks; /* SDK callback is not acoustic proof of success. */
}

static void bridge_task(void *arg)
{
    (void)arg;
    bridge_event event;
    for (;;) {
        /* Blocking on an RTOS queue leaves the CPU free for ASR and sensors. */
        if (xQueueReceive(bridge_events, &event, portMAX_DELAY) != pdPASS) continue;
        if (xTaskGetTickCount() - event.arrived > pdMS_TO_TICKS(500)) { ++dropped; ++expired; continue; }
        if (event.kind == BRIDGE_TX) {
            if (bridge_send(event.frame) == 0) ++tx_frames;
            else ++dropped;
            mprintf("[WE-BRIDGE] tx id=%u sent=%lu drop=%lu\n", event.frame.id,
                    (unsigned long)tx_frames, (unsigned long)dropped);
        } else {
            const we_bridge_mapping *mapping = we_bridge_from_wire(event.frame.type, event.frame.id);
            if (!mapping) { ++unsupported; continue; }
            /* Never feed RX broadcasts into sys_asr_result_hook or robot motion.
             * A busy player is rejected instead of interrupting another owner. */
            if (prompt_is_playing() || get_audio_play_state() != AUDIO_PLAY_STATE_IDLE) {
                ++play_failed; ++busy_rejected; continue;
            }
            audio_play_set_vol_gain(40);
            if (prompt_play_by_cmd_id(mapping->command_id, -1, bridge_play_done, false) == 0)
                ++play_accepted;
            else ++play_failed;
            mprintf("[WE-BRIDGE] rx type=%u id=%u accept=%lu callbacks=%lu fail=%lu\n",
                    event.frame.type, event.frame.id, (unsigned long)play_accepted,
                    (unsigned long)play_callbacks, (unsigned long)play_failed);
        }
    }
}

int we_bridge_init(void)
{
    if (ready) return 0;
    init_phase = 1;
    /* Fail closed if someone packages a different command resource. */
    for (size_t i = 0; i < we_bridge_mapping_count; ++i) {
        const we_bridge_mapping *m = &we_bridge_mappings[i];
        cmd_handle_t h = cmd_info_find_command_by_id(m->command_id);
        if (!is_valid_cmd_handle(h)) { init_result = -1; return -1; }
        const char *phrase = cmd_info_get_command_string(h);
        if (!phrase || strcmp(phrase, m->phrase)) { init_result = -2; return -2; }
    }
    init_phase = 2;
    bridge_events = xQueueCreate(8, sizeof(bridge_event));
    if (!bridge_events) { init_result = -3; return -3; }
    if (xTaskCreate(bridge_task, "we_bridge", 384, NULL, 2, NULL) != pdPASS) {
        vQueueDelete(bridge_events); bridge_events = NULL; init_result = -4; return -4;
    }
    init_phase = 3;
    __eclic_irq_set_vector(UART1_IRQn, (int32_t)bridge_irq);
    eclic_irq_set_priority(UART1_IRQn, 6, 0);
    UARTInterruptConfig(UART1, UART_BaudRate115200);
    /* SDK's ENABLE masks; DISABLE unmasks. Never enable TX-empty IRQ here. */
    UART_IntMaskConfig(UART1, UART_TXInt, ENABLE);
    ready = 1;
    init_result = 0;
    init_phase = 4;
    mprintf("[WE-BRIDGE] build=3501 ready UART1=115200 mappings=%u gain_limit=40\n",
            (unsigned)we_bridge_mapping_count);
    return 0;
}

void we_bridge_asr_result(uint16_t command_id)
{
    ++asr_seen;
    last_asr = command_id;
    const we_bridge_mapping *m = we_bridge_from_asr(command_id);
    if (!ready || !m) return;
    bridge_event event = {BRIDGE_TX, {m->type, m->wire_id}, xTaskGetTickCount()};
    if (xQueueSend(bridge_events, &event, 0) != pdPASS) ++dropped;
}

/* Called by the existing ten-second monitor, never from an ISR or timer.
 * Values are independent observations, not an atomic hardware snapshot.
 * Short lines limit log cost; no codec, GPIO or reserved-register access.
 * Callback counts still do not prove acoustic output. */
void we_bridge_log_status(void)
{
    mprintf("[WE-STAT] build=3501 tick=%lu phase=%u init=%d ready=%d\n",
            (unsigned long)xTaskGetTickCount(), init_phase, init_result, ready);
    mprintf("[WE-STAT] bytes=%lu frames=%lu uart_err=%lu tx=%lu\n",
            (unsigned long)rx_bytes, (unsigned long)rx_frames,
            (unsigned long)uart_errors, (unsigned long)tx_frames);
    mprintf("[WE-STAT] drop=%lu expired=%lu unknown=%lu busy=%lu\n",
            (unsigned long)dropped, (unsigned long)expired,
            (unsigned long)unsupported, (unsigned long)busy_rejected);
    mprintf("[WE-STAT] accept=%lu callbacks=%lu failed=%lu\n",
            (unsigned long)play_accepted, (unsigned long)play_callbacks,
            (unsigned long)play_failed);
    mprintf("[WE-STAT] player=%d prompt=%d mute=%d wake=%d\n",
            (int)get_audio_play_state(), (int)prompt_is_playing(),
            (int)get_mute_voice_in_state(), (int)get_wakeup_state());
    mprintf("[WE-STAT] asr_seen=%lu last_asr=%lu\n",
            (unsigned long)asr_seen, (unsigned long)last_asr);
}
