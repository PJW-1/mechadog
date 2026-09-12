#ifndef WONDERECHO_VOICE_STREAM_H
#define WONDERECHO_VOICE_STREAM_H
#include <stddef.h>
#include <stdint.h>
/* Call once in the SDK startup task before starting audio capture. */
int we_stream_init(void);
/* Task-context hook at the SDK's 16 kHz post-resample output.
 * Copies input before returning; never retains the SDK buffer pointer. */
void we_stream_submit(const int16_t *samples, size_t count);
#endif
