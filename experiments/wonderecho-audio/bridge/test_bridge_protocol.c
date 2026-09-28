#include "we_bridge_protocol.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <string.h>

static unsigned feed_all(we_bridge_parser *p, const uint8_t *bytes, size_t n,
                         uint32_t tick, we_bridge_frame *last)
{
    unsigned count = 0;
    for (size_t i = 0; i < n; ++i) count += we_bridge_feed(p, bytes[i], tick + (uint32_t)i, last);
    return count;
}

int main(void)
{
    we_bridge_parser p = {0}; we_bridge_frame f = {0}; uint8_t out[5];
    const uint8_t forward[] = {0xaa,0x55,0,1,0xfb};
    assert(feed_all(&p, forward, 2, 0, &f) == 0);
    assert(feed_all(&p, forward+2, 3, 2, &f) == 1 && f.type == 0 && f.id == 1);
    we_bridge_encode(f, out); assert(!memcmp(out, forward, 5));
    const uint8_t concat[] = {0xaa,0x55,0,9,0xfb,0xaa,0x55,0xff,5,0xfb};
    assert(feed_all(&p, concat, sizeof(concat), 10, &f) == 2 && f.type == 0xff && f.id == 5);
    const uint8_t corrupt[] = {0xaa,0x55,0,0xaa,0x55,0,2,0xfb};
    assert(feed_all(&p, corrupt, sizeof(corrupt), 30, &f) == 1 && f.id == 2);
    const uint8_t garbage[] = {1,0xaa,0xaa,0x55,0x7e,8,0xfb,0xaa,0x55,0,9,0xfb};
    assert(feed_all(&p, garbage, sizeof(garbage), 50, &f) == 1 && f.id == 9);
    assert(feed_all(&p, forward, 3, 80, &f) == 0);
    assert(feed_all(&p, forward+3, 2, 250, &f) == 0); /* stale prefix */
    assert(feed_all(&p, forward, 5, 300, &f) == 1);
    p.used = 0;
    assert(feed_all(&p, forward, 3, UINT32_MAX-2, &f) == 0);
    assert(feed_all(&p, forward+3, 2, 0, &f) == 1); /* tick wrap */
    for (uint16_t id = 0; id < 65535; ++id) {
        const we_bridge_mapping *m = we_bridge_from_asr(id);
        if (m) { assert(m->type == 0); assert(we_bridge_from_wire(0,m->wire_id) == m); }
    }
    assert(we_bridge_from_asr(11)->wire_id == 1);
    assert(we_bridge_from_asr(12)->wire_id == 2);
    assert(we_bridge_from_asr(13)->wire_id == 3);
    assert(we_bridge_from_asr(14)->wire_id == 4);
    assert(we_bridge_from_asr(19)->wire_id == 9);
    assert(!we_bridge_from_asr(3)); /* waking must never move the robot */
    assert(!we_bridge_from_asr(150)); /* broadcast must never become recognition */
    assert(!we_bridge_from_wire(0,0xe0)); /* no invented volume command */
    assert(!we_bridge_from_wire(1,1));
    assert(we_bridge_from_wire(0xff,5)->command_id == 154);
    /* Deterministic noise never grows parser storage; a clean frame recovers. */
    uint32_t seed = 7;
    for (uint32_t i = 0; i < 100000; ++i) {
        seed = seed * 1664525u + 1013904223u;
        we_bridge_feed(&p, (uint8_t)(seed >> 24), 500+i, &f);
        assert(p.used < 5);
    }
    assert(feed_all(&p, forward, 5, 101000, &f) == 1 && f.id == 1);
    puts("PASS: golden frames, fragmentation, corruption recovery, timeouts/wrap, mappings, 100000 noise bytes");
    return 0;
}
