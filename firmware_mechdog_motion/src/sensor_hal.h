// WBS 4.1.4: acquisition-only HAL for the MechDog body sensors.
#ifndef MECHADOG_SENSOR_HAL_H
#define MECHADOG_SENSOR_HAL_H

#include <stdint.h>

#include "motion_hal.h"  // Read the actuator default as well as build overrides.

#ifndef MECHADOG_ENABLE_SENSORS
#define MECHADOG_ENABLE_SENSORS 0
#endif

#if MECHADOG_ENABLE_SENSORS && MECHADOG_ENABLE_ACTUATORS
#error \
    "SensorHal and vendor actuators cannot run together: shared Wire/IMU ownership is not integrated"
#endif

namespace mechadog {

enum class SensorError : uint8_t {
  None,
  Disabled,
  Starting,
  TaskCreationFailed,
  SnapshotBusy,
  BusInitFailed,
  WrongIdentity,
  ConfigurationFailed,
  Calibrating,
  ReadFailed,
  InvalidReading,
  TimingFault,
  Stale,
};

const char* sensor_error_name(SensorError error);

// ADC characterization is distinct from validating the complete battery
// measurement against an external meter, including the board's resistor divider.
enum class BatteryAdcCalibration : uint8_t {
  Unavailable,
  EfuseTwoPoint,
  EfuseVref,
  DefaultVref,
};

const char* battery_adc_calibration_name(BatteryAdcCalibration source);

enum class SensorTimingFaultReason : uint8_t {
  None,
  WakeLate,
  CycleElapsed,
  ScheduledElapsed,
};

const char* sensor_timing_fault_reason_name(SensorTimingFaultReason reason);

constexpr uint8_t kSensorTimingStageImuFilter = 1;
constexpr uint8_t kSensorTimingStageSonar = 2;
constexpr uint8_t kSensorTimingStageAdc = 4;

struct SensorTimingFault {
  SensorTimingFaultReason reason = SensorTimingFaultReason::None;
  uint32_t fault_tick_ms = 0;  // FreeRTOS tick time, converted to milliseconds.
  uint32_t wake_late_ms = 0;   // Lateness at the start of the faulting cycle.
  uint32_t cycle_elapsed_ms = 0;
  // Wall time includes preemption and driver waits; it does not identify a
  // hardware fault or distinguish I2C waiting from CPU scheduling delays.
  uint64_t imu_filter_us = 0;
  uint64_t sonar_us = 0;
  uint64_t adc_us = 0;
  // Unset means not measured before the fault decision, rather than zero cost.
  uint8_t stages_measured_mask = 0;
};

struct SensorSnapshot {
  // A numeric default is never evidence of a measurement. Check each valid bit.
  bool task_started = false;
  bool imu_valid = false;
  bool dist_valid = false;
  bool batt_valid = false;
  float pitch = 0.0f;  // Vendor sensor axes, degrees; body-axis verification pending.
  float roll = 0.0f;
  float yaw = 0.0f;  // [0, 360); filter-relative orientation, not magnetic heading.
  float accel_g[3] = {};
  float gyro_dps[3] = {};
  float dist_cm = 0.0f;
  float batt_v = 0.0f;
  uint16_t battery_raw = 0;
  uint32_t battery_adc_mv = 0;  // GPIO34 voltage from the same raw sample.
  BatteryAdcCalibration battery_adc_calibration = BatteryAdcCalibration::Unavailable;
  bool axes_verified = false;
  bool battery_calibrated = false;  // External meter verification is still pending.
  bool yaw_relative = true;
  uint32_t imu_age_ms = UINT32_MAX;
  uint32_t dist_age_ms = UINT32_MAX;
  uint32_t batt_age_ms = UINT32_MAX;
  SensorError imu_error = SensorError::Disabled;
  SensorError dist_error = SensorError::Disabled;
  SensorError batt_error = SensorError::Disabled;
  SensorTimingFault timing_fault;  // First timing fault only; retained until reboot.

  // Acquisition validity is separate from body-axis and voltage calibration.
  bool all_valid() const { return imu_valid && dist_valid && batt_valid; }
};

// One physical sensor set; instantiate once. begin() only starts the dedicated
// task: true means task creation succeeded, not that any sensor passed its test.
// All Wire/ADC operations, including initialization, occur inside that task.
class SensorHal {
 public:
  bool begin();
  // Nonblocking snapshot copy. Contention or stale data yields valid=false.
  SensorSnapshot snapshot(uint32_t now_ms) const;
  static bool enabled() { return MECHADOG_ENABLE_SENSORS != 0; }
};

}  // namespace mechadog

#endif  // MECHADOG_SENSOR_HAL_H
