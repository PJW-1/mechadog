#ifndef WONDERECHO_VOICE_ENCODER_H
#define WONDERECHO_VOICE_ENCODER_H
#include <stddef.h>
#include <stdint.h>

/* 16 kHz mono, one complete 20 ms frame. Single owning task only.
 * Prototype codec adapter: does not initialize pins, record, or transmit. */
#define WE_PCM_SAMPLES 320
#define WE_PACKET_CAPACITY 144
int we_encoder_init(void);
void we_encoder_close(void);
/* Output: SDK AIOT 16-byte header + length-prefixed Speex.
 * Returns packet bytes, or a negative error. Never truncates a PCM frame. */
int we_encode_frame(const int16_t *pcm, size_t samples,
                    uint16_t sequence, uint8_t *packet, size_t capacity);
#endif
