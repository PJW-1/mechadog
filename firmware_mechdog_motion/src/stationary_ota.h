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
// Optional, actuator-OFF maintenance updater. No motor or Wire operations.
bool beginStationaryOta();
void pollStationaryOta(bool wifi_connected, const SensorSnapshot& sample);
}  // namespace mechadog
#endif
