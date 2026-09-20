#ifndef WE_BRIDGE_H
#define WE_BRIDGE_H
#include <stdint.h>
int we_bridge_init(void);
void we_bridge_log_status(void);
void we_bridge_asr_result(uint16_t command_id);
#endif
