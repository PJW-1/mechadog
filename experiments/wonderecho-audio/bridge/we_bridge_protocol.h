#ifndef WE_BRIDGE_PROTOCOL_H
#define WE_BRIDGE_PROTOCOL_H
#include <stddef.h>
#include <stdint.h>

typedef struct { uint8_t type, id; } we_bridge_frame;
typedef struct { uint8_t bytes[5], used; uint32_t last_ms; } we_bridge_parser;
typedef struct {
    uint8_t type, wire_id;
    uint16_t command_id;
    const char *phrase;
} we_bridge_mapping;

/* Explicit, reviewed subset. Unknown functional/learning IDs fail closed. */
extern const we_bridge_mapping we_bridge_mappings[];
extern const size_t we_bridge_mapping_count;
int we_bridge_feed(we_bridge_parser *p, uint8_t byte, uint32_t now_ms,
                   we_bridge_frame *frame);
const we_bridge_mapping *we_bridge_from_wire(uint8_t type, uint8_t id);
const we_bridge_mapping *we_bridge_from_asr(uint16_t command_id);
void we_bridge_encode(we_bridge_frame frame, uint8_t out[5]);
#endif
