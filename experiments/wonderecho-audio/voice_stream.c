#include "voice_stream.h"
#include "voice_encoder.h"
#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "ci130x_uart.h"
#include "ci130x_core_eclic.h"
#include "sdk_default_config.h"
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

void UART0_IRQHandler(void)
{
    static uint8_t command[16];
    static unsigned used;
    BaseType_t wake = pdFALSE;
    if (UART_MaskIntState(UART0, UART_TXInt)) {
        UART_IntMaskConfig(UART0, UART_TXInt, ENABLE);
        UART_IntClear(UART0, UART_TXInt);
        if (tx_space) xSemaphoreGiveFromISR(tx_space, &wake);
    }
    /* Bounded work even if the host sends noise continuously. */
    for (unsigned n = 0; n < 64 && !UART_FLAGSTAT(UART0, UART_RXFE); ++n) {
        uint32_t raw = UART0->UARTRdDR; /* Read each FIFO entry exactly once. */
        if (raw & 0xF00) { used = 0; continue; }
        command[used++] = (uint8_t)raw;
        if (used != sizeof(command)) continue;
        const uint8_t fixed[] = {0xa5,0xa5,0x5a,0x5a,0,0};
        const uint8_t tail[] = {0,0,0,1,0x78,0x56,0x34,0x12};
        uint32_t event = 0;
        if (!memcmp(command, fixed, 6) && !memcmp(command + 8, tail, 8) && command[7] == 1) {
            if (command[6] == 8) event = EV_START;
            if (command[6] == 6) event = EV_STOP;
            if (command[6] == 2) event = EV_QUERY;
#if WE_HOST_PROMPT_ONLY
            if (command[6] == 11) event = EV_PROMPT;
#endif
        }
        if (event) {
            if (worker) xTaskNotifyFromISR(worker, event, eSetBits, &wake);
            used = 0;
        } else {
            memmove(command, command + 1, 15);
            used = 15;
        }
    }
    UART_IntClear(UART0, UART_RXInt);
    portYIELD_FROM_ISR(wake);
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
#endif
    eclic_irq_set_priority(UART0_IRQn, 6, 0);
    UARTInterruptConfig(UART0, UART_BaudRate115200);
    for (;;) {
        uint32_t events = 0;
        xTaskNotifyWait(0, UINT32_MAX, &events, ms_ticks(100));
#if WE_HOST_PROMPT_ONLY
        if ((events & EV_STOP) && prompting) {
            we_prompt_cancel(); prompting = 0; prompt_fault = 1;
        }
        if ((events & EV_PROMPT) && !(events & EV_STOP) && !active && !prompting) {
            if (prompt_fault) status_packet(WE_FAILED, 8, 0);
            else {
                stop_capture(); sent = 0;
                if (we_prompt_start(worker, EV_PROMPT_DONE) != 0) status_packet(WE_FAILED, 8, 0);
                else {
                    prompting = 1; prompt_start = xTaskGetTickCount();
                    if (!status_packet(5, WE_OK, 0)) {
                        we_prompt_cancel(); prompting = 0; prompt_fault = 1;
                    }
                }
            }
        }
        if (prompting) {
            int done = we_prompt_result();
            if (done == 1) {
                prompting = 0;
                if (status_packet(6, WE_OK, 0)) events |= EV_START;
            } else if (done < 0 || xTaskGetTickCount() - prompt_start >= ms_ticks(5000)) {
                we_prompt_cancel(); prompting = 0; prompt_fault = 1;
                status_packet(WE_FAILED, 8, 0);
            }
            if (prompting) continue; /* No microphone capture during the prompt. */
        }
#endif
        if (events & EV_STOP) {
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
