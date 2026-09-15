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
// True while the body is guaranteed stationary for a flash write: always in
// actuator-OFF builds, in actuator builds only while SERVICE mode has parked
// it (motion blocked AND the loop watchdog armed). Defined by the sketch;
// the update handler rejects while it returns false.
bool serviceModeParked();
// Confirm path: after an update reboot the new image is pending-verify and
// the safe latch is still on — and the sketch refuses RESET_SAFE while
// pending — so a latched actuator build is as stationary as a parked one.
bool otaParkedForReboot();
// True while the running image awaits /confirm (ESP_OTA_IMG_PENDING_VERIFY).
bool otaPendingVerify();
}  // namespace mechadog
#endif
