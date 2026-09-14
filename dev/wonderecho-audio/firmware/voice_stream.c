#include "voice_stream.h"
#include "voice_encoder.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "ci130x_uart.h"
#include "ci130x_core_eclic.h"
#include "sdk_default_config.h"
#include "codec_manager.h"
#include "board.h"
#include "ci130x_pdm.h"
#include "ci130x_scu.h"
#include "ci130x_gpio.h"
#include <string.h>
#if WE_HOST_PROMPT_ONLY
#include "voice_prompt.h"
#endif

/* WonderEcho V3 USB UART0 verified by WEC1 captures on 2026-09-13. */
#if CONFIG_CI_LOG_UART || MSG_COM_USE_UART_EN || COMMAND_LINE_CONSOLE_EN
#error "Stream candidate must exclusively own UART0; disable SDK log/command UARTs"
#endif
#define EV_START 1u
#define EV_STOP 2u
#define EV_QUERY 4u
#define EV_AUDIO 8u
#define EV_PROMPT 16u
#define EV_PROMPT_DONE 32u
#define EV_LOOP 64u
#define EV_RXDATA 128u
#define QUEUE_FRAMES 8
#define MAX_FRAMES 250u /* Five-second bench capture, not indefinite recording. */
enum { WE_READY = 1, WE_STARTED = 2, WE_FINISHED = 3, WE_FAILED = 4 };
enum { WE_OK = 0, WE_STOP = 1, WE_LIMIT = 2, WE_OVERFLOW = 3, WE_CODEC = 4,
       WE_TX_TIMEOUT = 5, WE_CAPTURE_TIMEOUT = 6, WE_BAD_INPUT = 7 };
typedef struct { uint16_t sequence; int16_t pcm[WE_PCM_SAMPLES]; } Frame;
static QueueHandle_t frames;
static SemaphoreHandle_t tx_space;
static TaskHandle_t worker;
static volatile int accepting, capture_end;
static Frame partial;
static size_t partial_count;
static uint16_t produced;
static volatile uint32_t queue_peak;
static uint32_t encode_peak, send_peak;

static TickType_t ms_ticks(uint32_t ms)
{
    TickType_t n = pdMS_TO_TICKS(ms);
    return n ? n : 1;
}

void we_stream_submit(const int16_t *samples, size_t count)
{
    if (!worker) return;
    taskENTER_CRITICAL();
    if (accepting && (!samples || count != AUDIO_CAP_POINT_NUM_PER_FRM)) {
        accepting = 0;
        capture_end = WE_BAD_INPUT;
    }
    /* Copy bounded chunks under the lock: START/WE_STOP cannot race a partial frame. */
    while (accepting && count) {
        size_t n = WE_PCM_SAMPLES - partial_count;
        if (n > count) n = count;
        memcpy(partial.pcm + partial_count, samples, n * sizeof(*samples));
        partial_count += n; samples += n; count -= n;
        if (partial_count == WE_PCM_SAMPLES) {
            partial.sequence = produced;
            if (xQueueSend(frames, &partial, 0) != pdPASS) {
                accepting = 0; capture_end = WE_OVERFLOW;
            } else if (++produced == MAX_FRAMES) {
                accepting = 0; capture_end = WE_LIMIT;
            }
            uint32_t queued = uxQueueMessagesWaiting(frames);
            if (queued > queue_peak) queue_peak = queued;
            partial_count = 0;
        }
    }
    int changed = accepting || capture_end;
    taskEXIT_CRITICAL();
    if (changed) xTaskNotify(worker, EV_AUDIO, eSetBits);
}

/* Interrupts signal FIFO space; the encoder task never spins waiting for UART. */
static int send_bytes(const uint8_t *data, size_t size)
{
    size_t offset = 0;
    TickType_t started = xTaskGetTickCount(), budget = ms_ticks(40);
    while (offset < size) {
        while (offset < size && !UART_FLAGSTAT(UART0, UART_TXFF))
            UART_TXDATAConfig(UART0, data[offset++]);
        if (offset == size) break;
        TickType_t elapsed = xTaskGetTickCount() - started;
        if (elapsed >= budget) return 0;
        UART_IntMaskConfig(UART0, UART_TXInt, DISABLE); /* SDK DISABLE = unmask */
        if (xSemaphoreTake(tx_space, budget - elapsed) != pdTRUE) {
            UART_IntMaskConfig(UART0, UART_TXInt, ENABLE);
            return 0;
        }
    }
    UART_IntMaskConfig(UART0, UART_TXInt, ENABLE);
    return 1;
}

static void put16(uint8_t *p, uint16_t n) { p[0] = n; p[1] = n >> 8; }
static void put32(uint8_t *p, uint32_t n)
{
    put16(p, (uint16_t)n); put16(p + 2, (uint16_t)(n >> 16));
}

static int status_packet(uint16_t phase, uint16_t reason, uint32_t sent)
{
    uint8_t p[32] = {0xa5,0xa5,0x5a,0x5a};
    put16(p + 6, 0x0102); put16(p + 8, 16); put16(p + 10, 0x0100);
    put32(p + 12, 0x12345678);
    memcpy(p + 16, "WEC1", 4);
    put16(p + 20, phase); put16(p + 22, reason);
    put32(p + 24, sent); put32(p + 28, 16000);
    uint16_t sum = 0;
    for (unsigned i = 16; i < sizeof(p); ++i) sum += p[i];
    put16(p + 4, sum);
    return send_bytes(p, sizeof(p));
}

/* One bounded diagnostic packet per capture; timings include task preemption. */
static void diagnostic_packet(uint32_t elapsed, uint32_t sent, uint32_t reason)
{
    uint8_t p[48] = {0xa5,0xa5,0x5a,0x5a};
    put16(p + 6, 0x0109); put16(p + 8, 32); put16(p + 10, 0x0100);
    put32(p + 12, 0x12345678);
    put32(p + 16, elapsed); put32(p + 20, encode_peak); put32(p + 24, send_peak);
    put32(p + 28, queue_peak); put32(p + 32, produced); put32(p + 36, sent);
    put32(p + 40, configTICK_RATE_HZ); put32(p + 44, reason);
    uint16_t sum = 0;
    for (unsigned i = 16; i < sizeof(p); ++i) sum += p[i];
    put16(p + 4, sum);
    send_bytes(p, sizeof(p));
}

#if WE_HOST_PROMPT_ONLY
static void prompt_diagnostic_packet(void)
{
    uint32_t values[16];
    uint8_t p[80] = {0xa5,0xa5,0x5a,0x5a};
    we_prompt_snapshot(values);
    put16(p + 6, 0x010c); put16(p + 8, 64); put16(p + 10, 0x0100);
    put32(p + 12, 0x12345678);
    for (unsigned i = 0; i < 16; ++i) put32(p + 16 + 4*i, values[i]);
    uint16_t sum = 0;
    for (unsigned i = 16; i < sizeof(p); ++i) sum += p[i];
    put16(p + 4, sum); send_bytes(p, sizeof(p));
}

/* Analog output chain readback: PA polarity, PC4 pad, CODEC DAC registers. */
static void prompt_hw_packet(const uint32_t *values)
{
    uint8_t p[80] = {0xa5,0xa5,0x5a,0x5a};
    put16(p + 6, 0x010d); put16(p + 8, 64); put16(p + 10, 0x0100);
    put32(p + 12, 0x12345678);
    for (unsigned i = 0; i < 16; ++i) put32(p + 16 + 4*i, values[i]);
    uint16_t sum = 0;
    for (unsigned i = 16; i < sizeof(p); ++i) sum += p[i];
    put16(p + 4, sum); send_bytes(p, sizeof(p));
}
#endif

/* ---- Host->module audio streaming (v23) ----------------------------------
   Wire format: same 16-byte header as every other packet
   (a5a5 5a5a | sum | type | len | ver | 0x12345678), then `len` payload bytes.
   type 0x0110 PLAY_DATA carries raw PCM16LE mono 16 kHz; type 0x0111 PLAY_END
   (len 0) flushes the partial block, drains the DMA ring, mutes the amp and
   reports WE_FINISHED. Playback lazily starts on the first PLAY_DATA packet. */
#define RX_RING_SIZE 16384u          /* ~178 ms of headroom at 921600 baud */
#define PLAY_BLOCK_SIZE 1024u        /* 512 samples = 32 ms per DMA block */
#define PLAY_BLOCK_NUM 2u            /* two DMA blocks per queued buffer */
#define PLAY_BUF_COUNT 24u           /* 24 x 2048 B = 48 KB = 1.5 s of audio */
#define PLAY_BUF_BYTES (PLAY_BLOCK_SIZE * PLAY_BLOCK_NUM)
#define PLAY_MAX_PAYLOAD 2048u
#define PLAY_WARMUP_MS 400u          /* measured TC8002D wake time (v22: ~400 ms) */

/* Same diagnostics the prompt path exposes; reset per stream for clean reads. */
extern volatile uint32_t we_output_irqs, we_tx_peak;

static uint8_t rx_ring[RX_RING_SIZE];
static volatile uint32_t rx_head, rx_tail;
static uint32_t rx_dropped, rx_bad_packets;

static uint8_t play_pool[PLAY_BUF_COUNT * PLAY_BUF_BYTES];
static int play_state;             /* 0 idle, 1 warming, 2 playing */
static uint8_t *play_cur;
static uint32_t play_fill;
static TickType_t play_pa_on, play_last_data;
static uint32_t play_bytes_in, play_bufs_in, play_underrun, play_level_peak;
static int play_cfg_rc, play_start_rc;
static volatile int we_prompting;  /* flash-prompt owns codec 1 output */

static void play_abort(void)
{
    if (play_state) {
        cm_stop_codec(1, CODEC_OUTPUT);
        power_amplifier_off();
    }
    play_state = 0; play_cur = 0; play_fill = 0;
}

static void play_begin(void)
{
    cm_pcm_buffer_info_t bi;
    memset(&bi, 0, sizeof(bi));
    bi.play_buffer_info.block_num = PLAY_BLOCK_NUM;
    bi.play_buffer_info.buffer_num = PLAY_BUF_COUNT;
    bi.play_buffer_info.block_size = PLAY_BLOCK_SIZE;
    bi.play_buffer_info.buffer_size = PLAY_BUF_BYTES;
    bi.play_buffer_info.pcm_buffer = play_pool;
    cm_config_pcm_buffer(1, CODEC_OUTPUT, &bi);
    play_last_data = xTaskGetTickCount();
    cm_sound_info_t si = { .sample_rate = 16000, .channel_flag = 1,
                           .sample_depth = IIS_DW_16BIT };
    play_cfg_rc = cm_config_codec(1, CODEC_OUTPUT, &si);
    /* Same analog chain the prompt path needs: PDM block powers the HPOUT
       driver, and reg2d DAC gain is only ever set via cm_set_codec_dac_gain
       (fresh config leaves it at the power-up default). */
    scu_set_device_gate((uint32_t)PDM, ENABLE);
    scu_set_device_reset((uint32_t)PDM);
    scu_set_device_reset_release((uint32_t)PDM);
    pdm_power_up(PDM_CURRENT_128I);
    pdm_hpout_mute_disable();
    cm_set_codec_dac_gain(1, 0, 40);
    we_output_irqs = 0; we_tx_peak = 0;
    power_amplifier_on();
    play_pa_on = xTaskGetTickCount();
    play_state = 1;
    play_bytes_in = play_bufs_in = play_underrun = play_level_peak = 0;
    play_start_rc = -1;
}

/* Pull `len` payload bytes out of the ring into playback buffers. */
static void play_consume(uint32_t len)
{
    play_last_data = xTaskGetTickCount();
    play_bytes_in += len;
    while (len) {
        if (!play_cur) {
            uint32_t buf = 0;
            cm_get_pcm_buffer(1, &buf, 0);
            if (!buf) {          /* all buffers queued — drop this payload only */
                rx_tail = (rx_tail + len) % RX_RING_SIZE;
                ++play_underrun; return;
            }
            play_cur = (uint8_t *)buf; play_fill = 0;
        }
        uint32_t n = PLAY_BUF_BYTES - play_fill;
        if (n > len) n = len;
        uint32_t tail = rx_tail;
        for (uint32_t i = 0; i < n; ++i) {
            play_cur[play_fill + i] = rx_ring[tail];
            tail = (tail + 1) % RX_RING_SIZE;
        }
        rx_tail = tail;
        play_fill += n; len -= n;
        if (play_fill == PLAY_BUF_BYTES) {
            cm_write_codec(1, play_cur, 0);
            ++play_bufs_in; play_cur = 0;
            uint32_t lvl = cm_output_busy_count(1);
            if (lvl > play_level_peak) play_level_peak = lvl;
        }
    }
}

/* One play diagnostic packet per stream; mirrors diagnostic_packet framing. */
static void play_diagnostic_packet(void)
{
    uint8_t p[56] = {0xa5,0xa5,0x5a,0x5a};
    put16(p + 6, 0x0112); put16(p + 8, 40); put16(p + 10, 0x0100);
    put32(p + 12, 0x12345678);
    put32(p + 16, play_bytes_in); put32(p + 20, play_bufs_in);
    put32(p + 24, play_underrun); put32(p + 28, rx_dropped);
    put32(p + 32, we_output_irqs); put32(p + 36, we_tx_peak);
    put32(p + 40, (uint32_t)play_cfg_rc); put32(p + 44, (uint32_t)play_start_rc);
    put32(p + 48, play_level_peak); put32(p + 52, rx_bad_packets);
    uint16_t sum = 0;
    for (unsigned i = 16; i < sizeof(p); ++i) sum += p[i];
    put16(p + 4, sum);
    send_bytes(p, sizeof(p));
}

static void play_finish(void)
{
    if (play_cur) {                 /* zero-pad the partial buffer */
        memset(play_cur + play_fill, 0, PLAY_BUF_BYTES - play_fill);
        cm_write_codec(1, play_cur, 0);
        ++play_bufs_in; play_cur = 0; play_fill = 0;
    }
    if (play_state) {
        int was_playing = play_state == 2;
        play_state = 0;
        /* Drain first: cm_stop_codec would discard buffers still queued. */
        TickType_t deadline = xTaskGetTickCount() + ms_ticks(2500);
        while (was_playing && cm_output_busy_count(1)
               && xTaskGetTickCount() < deadline)
            vTaskDelay(ms_ticks(10));
        cm_stop_codec(1, CODEC_OUTPUT);
        power_amplifier_off();
        status_packet(WE_FINISHED, WE_OK, play_bytes_in);
        play_diagnostic_packet();
    }
}

void UART0_IRQHandler(void)
{
    BaseType_t wake = pdFALSE;
    if (UART_MaskIntState(UART0, UART_TXInt)) {
        UART_IntMaskConfig(UART0, UART_TXInt, ENABLE);
        UART_IntClear(UART0, UART_TXInt);
        if (tx_space) xSemaphoreGiveFromISR(tx_space, &wake);
    }
    /* Drain the FIFO into the ring; the worker parses packets. */
    int got = 0;
    for (unsigned n = 0; n < 64 && !UART_FLAGSTAT(UART0, UART_RXFE); ++n) {
        uint32_t raw = UART0->UARTRdDR; /* Read each FIFO entry exactly once. */
        if (raw & 0xF00) continue;
        uint32_t next = (rx_head + 1) % RX_RING_SIZE;
        if (next != rx_tail) { rx_ring[rx_head] = (uint8_t)raw; rx_head = next; got = 1; }
        else ++rx_dropped;
    }
    if (got && worker) xTaskNotifyFromISR(worker, EV_RXDATA, eSetBits, &wake);
    UART_IntClear(UART0, UART_RXInt);
    portYIELD_FROM_ISR(wake);
}

static uint32_t rx_avail(void)
{
    return (rx_head + RX_RING_SIZE - rx_tail) % RX_RING_SIZE;
}

/* Parse framed packets out of the ring; commands map to the same event bits
   the old sliding-window matcher produced. */
static uint32_t drain_rx_ring(void)
{
    static uint8_t hdr[16];
    uint32_t events = 0;
    for (;;) {
        if (rx_avail() < 16) return events;
        for (unsigned i = 0; i < 16; ++i)
            hdr[i] = rx_ring[(rx_tail + i) % RX_RING_SIZE];
        if (hdr[0] != 0xa5 || hdr[1] != 0xa5 || hdr[2] != 0x5a || hdr[3] != 0x5a ||
            hdr[7] != 1 || hdr[10] != 0 || hdr[11] != 1 ||
            hdr[12] != 0x78 || hdr[13] != 0x56 || hdr[14] != 0x34 || hdr[15] != 0x12) {
            rx_tail = (rx_tail + 1) % RX_RING_SIZE;
            continue;
        }
        uint32_t len = (uint32_t)hdr[8] | ((uint32_t)hdr[9] << 8);
        if (len > PLAY_MAX_PAYLOAD) {
            rx_tail = (rx_tail + 1) % RX_RING_SIZE;
            continue;
        }
        if (rx_avail() < 16 + len) return events;
        /* Payload checksum (host: sum(payload) & 0xffff). A skipped/corrupt
           UART byte would otherwise reach the speaker as loud noise. */
        uint32_t sum = 0;
        for (uint32_t i = 0; i < len; ++i)
            sum += rx_ring[(rx_tail + 16 + i) % RX_RING_SIZE];
        if ((sum & 0xffffu) != ((uint32_t)hdr[4] | ((uint32_t)hdr[5] << 8))) {
            rx_tail = (rx_tail + 16 + len) % RX_RING_SIZE;
            ++rx_bad_packets;
            continue;
        }
        rx_tail = (rx_tail + 16) % RX_RING_SIZE;
        if (hdr[6] == 0x10 && len) {             /* PLAY_DATA */
            if (!play_state && !we_prompting) play_begin();
            if (play_state) play_consume(len);
            else rx_tail = (rx_tail + len) % RX_RING_SIZE;
        } else {
            rx_tail = (rx_tail + len) % RX_RING_SIZE;
            if (hdr[6] == 0x11) events |= 0x10000u;      /* internal: play end */
            if (hdr[6] == 8) events |= EV_START;
            if (hdr[6] == 6) events |= EV_STOP;
            if (hdr[6] == 2) events |= EV_QUERY;
#if WE_HOST_PROMPT_ONLY
            if (hdr[6] == 11) events |= EV_PROMPT;
            if (hdr[6] == 14) events |= EV_LOOP;
#endif
        }
    }
}

static void stop_capture(void)
{
    taskENTER_CRITICAL();
    accepting = 0; capture_end = 0; partial_count = 0;
    taskEXIT_CRITICAL();
    xQueueReset(frames);
    we_encoder_close();
}

static void stream_task(void *unused)
{
    (void)unused;
    Frame frame;
    uint8_t packet[WE_PACKET_CAPACITY];
    int active = 0;
    uint32_t sent = 0;
    TickType_t last_audio = 0, session_start = 0;
#if WE_HOST_PROMPT_ONLY
    int prompting = 0, prompt_fault = 0;
    TickType_t prompt_start = 0;
    uint32_t mid_hw[16];
    int mid_hw_taken = 0;
#endif
    /* Pre-charge the codec-1 output path once at boot. On a cold start the
       DAC bias / HPOUT coupling take a while to ramp, which ate the head of
       the first stream; with IF_JUST_CLOSE_HPOUT_WHILE_NO_PLAY the bias then
       stays charged for later streams. The pool is .bss zeros, so this plays
       silence while the amplifier stays off (no boot pop). */
    {
        cm_pcm_buffer_info_t bi;
        memset(&bi, 0, sizeof(bi));
        bi.play_buffer_info.block_num = PLAY_BLOCK_NUM;
        bi.play_buffer_info.buffer_num = PLAY_BUF_COUNT;
        bi.play_buffer_info.block_size = PLAY_BLOCK_SIZE;
        bi.play_buffer_info.buffer_size = PLAY_BUF_BYTES;
        bi.play_buffer_info.pcm_buffer = play_pool;
        if (cm_config_pcm_buffer(1, CODEC_OUTPUT, &bi) == 0) {
            cm_sound_info_t si = { .sample_rate = 16000, .channel_flag = 1,
                                   .sample_depth = IIS_DW_16BIT };
            cm_config_codec(1, CODEC_OUTPUT, &si);
            scu_set_device_gate((uint32_t)PDM, ENABLE);
            scu_set_device_reset((uint32_t)PDM);
            scu_set_device_reset_release((uint32_t)PDM);
            pdm_power_up(PDM_CURRENT_128I);
            pdm_hpout_mute_disable();
            cm_set_codec_dac_gain(1, 0, 40);
            cm_start_codec(1, CODEC_OUTPUT);
            vTaskDelay(ms_ticks(1500));
            cm_stop_codec(1, CODEC_OUTPUT);
        }
    }
    eclic_irq_set_priority(UART0_IRQn, 6, 0);
    UARTInterruptConfig(UART0, UART_BaudRate921600);
    for (;;) {
        uint32_t events = 0;
        xTaskNotifyWait(0, UINT32_MAX, &events, ms_ticks(100));
        events |= drain_rx_ring();
#if WE_HOST_PROMPT_ONLY
        if ((events & EV_STOP) && prompting) {
            we_prompt_cancel(); prompting = 0; we_prompting = 0; prompt_fault = 1;
        }
        if ((events & EV_PROMPT) && !(events & EV_STOP) && !active && !prompting) {
            if (prompt_fault) status_packet(WE_FAILED, 8, 0);
            else {
                stop_capture(); sent = 0;
                if (we_prompt_start(worker, EV_PROMPT_DONE) != 0) status_packet(WE_FAILED, 8, 0);
                else {
                    prompting = 1; we_prompting = 1; prompt_start = xTaskGetTickCount();
                    mid_hw_taken = 0;
                    if (!status_packet(5, WE_OK, 0)) {
                        we_prompt_cancel(); prompting = 0; we_prompting = 0; prompt_fault = 1;
                    }
                }
            }
        }
        if ((events & EV_LOOP) && !(events & EV_STOP) && !active && !prompting) {
            if (prompt_fault) status_packet(WE_FAILED, 8, 0);
            else {
                stop_capture(); sent = 0; produced = 0;
                queue_peak = encode_peak = send_peak = 0;
                if (we_encoder_init() != 0) status_packet(WE_FAILED, WE_CODEC, sent);
                else if (we_prompt_start(worker, EV_PROMPT_DONE) != 0) {
                    we_encoder_close(); status_packet(WE_FAILED, 8, 0);
                } else if (!status_packet(WE_STARTED, WE_OK, 0)) {
                    we_prompt_cancel(); we_encoder_close();
                } else {
                    last_audio = session_start = prompt_start = xTaskGetTickCount();
                    active = 1; prompting = 1; we_prompting = 1; mid_hw_taken = 0;
                    taskENTER_CRITICAL(); accepting = 1; taskEXIT_CRITICAL();
                }
            }
        }
        if (prompting) {
            int done = we_prompt_result();
            if (!mid_hw_taken && xTaskGetTickCount() - prompt_start >= ms_ticks(400)) {
                we_prompt_hw_snapshot(mid_hw, 1); mid_hw_taken = 1;
            }
            if (done == 1) {
                uint32_t post_hw[16];
                /* v20: keep replaying the prompt while alternating the PC4
                   amp-enable level; each pass resets the timeout budget. */
                if (we_prompt_advance()) {
                    prompt_start = xTaskGetTickCount();
                    mid_hw_taken = 0;
                    continue;
                }
                prompting = 0; we_prompting = 0;
                prompt_diagnostic_packet();
                if (mid_hw_taken) prompt_hw_packet(mid_hw);
                we_prompt_hw_snapshot(post_hw, 2); prompt_hw_packet(post_hw);
                if (!active && status_packet(6, WE_OK, 0)) events |= EV_START;
            } else if (done < 0 || xTaskGetTickCount() - prompt_start >= ms_ticks(5000)) {
                uint32_t post_hw[16];
                prompt_diagnostic_packet();
                if (mid_hw_taken) prompt_hw_packet(mid_hw);
                we_prompt_hw_snapshot(post_hw, 2); prompt_hw_packet(post_hw);
                we_prompt_cancel(); prompting = 0; we_prompting = 0; prompt_fault = 1;
                if (!active) status_packet(WE_FAILED, 8, 0);
            }
            if (prompting && !active) continue; /* No microphone capture during the prompt. */
        }
#endif
        /* Streaming playback state machine (v23). */
        if (events & 0x10000u) play_finish();
        if (play_state == 1 && xTaskGetTickCount() - play_pa_on >= ms_ticks(PLAY_WARMUP_MS)) {
            play_start_rc = cm_start_codec(1, CODEC_OUTPUT);
            play_state = 2;
        }
        if (play_state &&
            xTaskGetTickCount() - play_last_data >= ms_ticks(3000))
            play_finish();      /* host went away mid-stream */
        if (events & EV_STOP) {
            play_abort();
            stop_capture(); active = 0;
            status_packet(WE_FINISHED, WE_STOP, sent);
        } else if ((events & EV_START) && !active) {
            stop_capture(); sent = 0; produced = 0;
            queue_peak = encode_peak = send_peak = 0;
            if (we_encoder_init() != 0) status_packet(WE_FAILED, WE_CODEC, sent);
            else if (!status_packet(WE_STARTED, WE_OK, sent)) we_encoder_close();
            else {
                last_audio = session_start = xTaskGetTickCount();
                active = 1;
                taskENTER_CRITICAL(); accepting = 1; taskEXIT_CRITICAL();
            }
        }
        if ((events & EV_QUERY) && !active) status_packet(WE_READY, WE_OK, 0);
        if (!active) continue;
        int reason = 0;
        /* Bound each batch; return to pending WE_STOP events after at most 8 frames. */
        for (unsigned i = 0; i < QUEUE_FRAMES && xQueueReceive(frames, &frame, 0) == pdPASS; ++i) {
            last_audio = xTaskGetTickCount();
            int bytes = we_encode_frame(frame.pcm, WE_PCM_SAMPLES, frame.sequence, packet, sizeof(packet));
            uint32_t encode_elapsed = xTaskGetTickCount() - last_audio;
            if (encode_elapsed > encode_peak) encode_peak = encode_elapsed;
            if (bytes < 0) { reason = WE_CODEC; break; }
            TickType_t tx_start = xTaskGetTickCount();
            if (!send_bytes(packet, (size_t)bytes)) { reason = WE_TX_TIMEOUT; break; }
            uint32_t tx_elapsed = xTaskGetTickCount() - tx_start;
            if (tx_elapsed > send_peak) send_peak = tx_elapsed;
            ++sent;
        }
        if (!reason && capture_end && (capture_end != WE_LIMIT || !uxQueueMessagesWaiting(frames)))
            reason = capture_end;
        TickType_t now = xTaskGetTickCount();
        if (!reason && (now - last_audio >= ms_ticks(1000) || now - session_start >= ms_ticks(7000)))
            reason = WE_CAPTURE_TIMEOUT;
        if (reason) {
            stop_capture(); active = 0;
            diagnostic_packet(now - session_start, sent, (uint32_t)reason);
            status_packet(reason == WE_LIMIT ? WE_FINISHED : WE_FAILED, reason, sent);
        }
    }
}

int we_stream_init(void)
{
    if (worker) return -1;
    frames = xQueueCreate(QUEUE_FRAMES, sizeof(Frame));
    tx_space = xSemaphoreCreateBinary();
    if (!frames || !tx_space) {
        if (frames) vQueueDelete(frames);
        if (tx_space) vSemaphoreDelete(tx_space);
        frames = 0; tx_space = 0;
        return -2;
    }
    /* Share priority 4 with the SDK capture task; keep timer service (5) above both.
     * Priority 3 capture diagnostics showed up to 52 ms wall time per 20 ms frame. */
    if (xTaskCreate(stream_task, "voice_stream", 2048, NULL, 4, &worker) != pdPASS) {
        vQueueDelete(frames); vSemaphoreDelete(tx_space);
        frames = 0; tx_space = 0;
        return -3;
    }
    return 0;
}

