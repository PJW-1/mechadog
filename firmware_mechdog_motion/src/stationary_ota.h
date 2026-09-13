#ifndef MECHADOG_STATIONARY_OTA_H
#define MECHADOG_STATIONARY_OTA_H

#ifndef MECHADOG_ENABLE_OTA
#define MECHADOG_ENABLE_OTA 0
#endif
#ifndef MECHADOG_OTA_VERSION
#define MECHADOG_OTA_VERSION "ota-dev"
#endif

namespace mechadog {
struct SensorSnapshot;
// Optional maintenance updater. No motor or Wire operations of its own.
// On actuator-capable builds the /firmware endpoint refuses while the runtime
// actuator gate is open — updates remain stationary-only.
bool beginStationaryOta();
void pollStationaryOta(bool wifi_connected, const SensorSnapshot& sample);
// +1 requests actuators on, -1 requests off, 0 means no pending request. The
// loop owner applies it (stop + latch) because the servo driver lives there.
int consumeOtaActuatorRequest();
}  // namespace mechadog
#endif
