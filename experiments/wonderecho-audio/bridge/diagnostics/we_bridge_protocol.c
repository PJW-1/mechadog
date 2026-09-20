#include "we_bridge_protocol.h"
#include <string.h>

/* Factory V00729 command resource 60000 + official MechDog/WonderEcho table.
 * Do not subtract a constant from arbitrary internal IDs (wake/internal IDs
 * would then be misreported as motion). Broadcast RX never becomes ASR TX.
 */
const we_bridge_mapping we_bridge_mappings[] = {
    {0x00, 1, 11, "GO-STRAIGHT"},
    {0x00, 2, 12, "GO-BACKWARD"},
    {0x00, 3, 13, "TURN-LEFT"},
    {0x00, 4, 14, "TURN-RIGHT"},
    {0x00, 9, 19, "STOP"},
    {0x00, 10, 20, "ATTENTION"},
    {0x00, 11, 21, "GET-DOWN"},
    {0x00, 12, 22, "SIT-DOWN"},
    {0x00, 26, 36, "HELLO"},
    {0x00, 27, 37, "INTRODUCE-YOURSELF"},
    {0x00, 28, 38, "SHOW-A-SKIII"},
    {0x00, 29, 39, "MARCH"},
    {0x00, 30, 40, "SHAKE-HEAD"},
    {0xff, 1, 150, "<RECYCLABLE-WASTE>"},
    {0xff, 2, 151, "<KITCHEN-WASTE>"},
    {0xff, 3, 152, "<HAZARDOUS-WASTE>"},
    {0xff, 4, 153, "<OTHER-WASTE>"},
    {0xff, 5, 154, "<ABSTACLE-AHEAD>"},
};
const size_t we_bridge_mapping_count = sizeof(we_bridge_mappings) / sizeof(we_bridge_mappings[0]);

const we_bridge_mapping *we_bridge_from_wire(uint8_t type, uint8_t id)
{
    for (size_t i = 0; i < we_bridge_mapping_count; ++i)
        if (we_bridge_mappings[i].type == type && we_bridge_mappings[i].wire_id == id)
            return &we_bridge_mappings[i];
    return NULL;
}

const we_bridge_mapping *we_bridge_from_asr(uint16_t command_id)
{
    for (size_t i = 0; i < we_bridge_mapping_count; ++i)
        if (we_bridge_mappings[i].type == 0 && we_bridge_mappings[i].command_id == command_id)
            return &we_bridge_mappings[i];
    return NULL;
}

void we_bridge_encode(we_bridge_frame frame, uint8_t out[5])
{
    out[0] = 0xaa; out[1] = 0x55; out[2] = frame.type; out[3] = frame.id; out[4] = 0xfb;
}

int we_bridge_feed(we_bridge_parser *p, uint8_t byte, uint32_t now_ms,
                   we_bridge_frame *frame)
{
    if ((uint32_t)(now_ms - p->last_ms) > 100u) p->used = 0;
    p->last_ms = now_ms;
    p->bytes[p->used++] = byte;
    /* Keep the longest suffix that can still be a frame prefix. Handles
     * embedded AA55, dropped bytes, bad tail/type, and back-to-back frames. */
    for (;;) {
        int bad = p->bytes[0] != 0xaa;
        if (p->used > 1 && p->bytes[1] != 0x55) bad = 1;
        if (p->used > 2 && p->bytes[2] != 0 && p->bytes[2] != 0xff) bad = 1;
        if (p->used == 5 && p->bytes[4] != 0xfb) bad = 1;
        if (!bad) break;
        --p->used;
        if (!p->used) return 0;
        memmove(p->bytes, p->bytes + 1, p->used);
    }
    if (p->used != 5) return 0;
    frame->type = p->bytes[2]; frame->id = p->bytes[3]; p->used = 0;
    return 1;
}
