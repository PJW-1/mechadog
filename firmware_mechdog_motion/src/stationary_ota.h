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
// Optional maintenance updater. No motor or Wire operations. Actuator-OFF
// builds are always parked; actuator builds are parked only in SERVICE mode.
bool beginStationaryOta();
void pollStationaryOta(bool wifi_connected, const SensorSnapshot& sample);
// True while the body is guaranteed stationary: always in actuator-OFF
// builds, in actuator builds only while SERVICE mode has parked it. Defined
// by the sketch; the update/confirm handlers reject while it returns false.
bool serviceModeParked();
}  // namespace mechadog
#endif
