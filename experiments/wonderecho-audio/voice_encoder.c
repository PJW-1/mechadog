#include "voice_encoder.h"
#include "speex.h"
#include "speex_bits.h"
#include "modes.h"
#include "sb_celp.h"

/* Same wideband encoder table as SDK 2.2.7 cias_voice_upload.c.
 * This is a speech codec, not a recognition model. Decoder entries are unused. */
extern const SpeexSBMode sb_wb_mode;
static SpeexMode wideband = {
    .mode = &sb_wb_mode, .query = wb_mode_query,
    .modeName = "wideband (sub-band CELP)", .modeID = 1,
    .bitstream_version = 4,
    .enc_init = sb_encoder_init, .enc_destroy = sb_encoder_destroy,
    .enc = sb_encode, .enc_ctl = sb_encoder_ctl
};
static void *encoder;
static SpeexBits bits;
static char bit_buffer[128];
static spx_int16_t input[WE_PCM_SAMPLES];

static void put16(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)value;
    out[1] = (uint8_t)(value >> 8);
}

void we_encoder_close(void)
{
    if (encoder) {
        speex_encoder_destroy(encoder);
        encoder = 0;
        speex_bits_destroy(&bits);
    }
}

int we_encoder_init(void)
{
    int quality = 5, frame_size = 0, rate = 16000;
#ifndef DISABLE_VBR
    int disabled = 0;
#endif
    if (encoder) return -1; /* Caller must close before resetting an utterance. */
    encoder = speex_encoder_init(&wideband);
    if (!encoder) return -2;
    speex_bits_init_buffer(&bits, bit_buffer, sizeof(bit_buffer));
    if (speex_encoder_ctl(encoder, SPEEX_SET_QUALITY, &quality) != 0 ||
        speex_encoder_ctl(encoder, SPEEX_SET_SAMPLING_RATE, &rate) != 0 ||
#ifndef DISABLE_VBR
        speex_encoder_ctl(encoder, SPEEX_SET_VBR, &disabled) != 0 ||
        speex_encoder_ctl(encoder, SPEEX_SET_VAD, &disabled) != 0 ||
        speex_encoder_ctl(encoder, SPEEX_SET_DTX, &disabled) != 0 ||
#endif
        speex_encoder_ctl(encoder, SPEEX_GET_FRAME_SIZE, &frame_size) != 0 ||
        frame_size != WE_PCM_SAMPLES) {
        we_encoder_close();
        return -3;
    }
    return 0;
}

int we_encode_frame(const int16_t *pcm, size_t samples,
                    uint16_t sequence, uint8_t *packet, size_t capacity)
{
    if (!encoder || !pcm || !packet || samples != WE_PCM_SAMPLES ||
        capacity < WE_PACKET_CAPACITY) return -1;
    for (size_t i = 0; i < WE_PCM_SAMPLES; ++i) input[i] = pcm[i];
    speex_bits_reset(&bits);
    if (speex_encode_int(encoder, input, &bits) < 0 || bits.overflow) {
        we_encoder_close();
        return -2;
    }
    int bytes = speex_bits_nbytes(&bits);
    if (bytes < 1 || bytes > 127 ||
        speex_bits_write(&bits, (char *)packet + 17, bytes) != bytes) {
        we_encoder_close();
        return -3;
    }
    packet[16] = (uint8_t)bytes;
    uint16_t checksum = 0;
    for (int i = 16; i < 17 + bytes; ++i) checksum += packet[i];
    packet[0] = 0xa5; packet[1] = 0xa5;
    packet[2] = 0x5a; packet[3] = 0x5a;
    put16(packet + 4, checksum);
    put16(packet + 6, 0x0105); /* PCM_MIDDLE: payload format selected by firmware. */
    put16(packet + 8, (uint16_t)(bytes + 1));
    put16(packet + 10, sequence);
    packet[12] = 0x78; packet[13] = 0x56;
    packet[14] = 0x34; packet[15] = 0x12;
    return bytes + 17;
}
