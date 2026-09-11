#include "sensor_hal.h"

#if MECHADOG_ENABLE_SENSORS
// Keep dependency includes visible to Arduino's library discovery. Wrapping
// them in __has_include prevents discovery before library search paths exist.
#include <Arduino.h>
#include <Wire.h>
#include <esp_adc_cal.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <math.h>

#include "MadgwickAHRS.h"
#include "SensorQMI8658.hpp"
#endif

namespace mechadog {

const char* sensor_error_name(SensorError error) {
  switch (error) {
    case SensorError::None:
      return "none";
    case SensorError::Disabled:
      return "disabled";
    case SensorError::Starting:
      return "starting";
    case SensorError::TaskCreationFailed:
      return "task_creation_failed";
    case SensorError::SnapshotBusy:
      return "snapshot_busy";
    case SensorError::BusInitFailed:
      return "bus_init_failed";
    case SensorError::WrongIdentity:
      return "wrong_identity";
    case SensorError::ConfigurationFailed:
      return "configuration_failed";
    case SensorError::Calibrating:
      return "calibrating";
    case SensorError::ReadFailed:
      return "read_failed";
    case SensorError::InvalidReading:
      return "invalid_reading";
    case SensorError::TimingFault:
      return "timing_fault";
    case SensorError::Stale:
      return "stale";
  }
  return "unknown";
}

const char* battery_adc_calibration_name(BatteryAdcCalibration source) {
  switch (source) {
    case BatteryAdcCalibration::Unavailable:
      return "unavailable";
    case BatteryAdcCalibration::EfuseTwoPoint:
      return "efuse_two_point";
    case BatteryAdcCalibration::EfuseVref:
      return "efuse_vref";
    case BatteryAdcCalibration::DefaultVref:
      return "default_vref_1100";
  }
  return "unknown";
}

const char* sensor_timing_fault_reason_name(SensorTimingFaultReason reason) {
  switch (reason) {
    case SensorTimingFaultReason::None:
      return "none";
    case SensorTimingFaultReason::WakeLate:
      return "wake_late";
    case SensorTimingFaultReason::CycleElapsed:
      return "cycle_elapsed";
    case SensorTimingFaultReason::ScheduledElapsed:
      return "scheduled_elapsed";
  }
  return "unknown";
}

#if MECHADOG_ENABLE_SENSORS
namespace {

constexpr int kSdaPin = 22;
constexpr int kSclPin = 23;
constexpr int kBatteryPin = 34;
constexpr uint32_t kDefaultAdcVrefMv = 1100;  // ESP32 Arduino 2.0.12 fallback.
// Official MechDog V1.0/V1.2: VIN -- R1 30k -- ADC_BAT -- R5 10k -- GND.
constexpr float kBatteryDividerRatio = 4.0f;
constexpr uint8_t kImuAddress = 0x6A;
constexpr uint8_t kSonarAddress = 0x77;
constexpr uint32_t kSamplePeriodMs = 40;  // Official Madgwick filter.begin(25).
constexpr uint32_t kMaxAgeMs = 200;
constexpr uint32_t kCalibrationTimeoutMs = 2000;
constexpr uint8_t kCalibrationSamples = 11;  // First sample + ten pairwise averages.

struct AcquisitionRecord {
  SensorSnapshot value;
  uint32_t imu_sample_ms = 0;
  uint32_t dist_sample_ms = 0;
  uint32_t batt_sample_ms = 0;
  bool imu_seen = false;
  bool dist_seen = false;
  bool batt_seen = false;
};

SensorQMI8658 g_qmi;
Madgwick g_filter;
esp_adc_cal_characteristics_t g_battery_adc_chars = {};
SemaphoreHandle_t g_snapshot_mutex = nullptr;
TaskHandle_t g_sensor_task = nullptr;
AcquisitionRecord g_published;
SensorError g_start_error = SensorError::Starting;

// This task exclusively owns Wire. The compile-time actuator exclusion prevents
// the vendor's independent IMU task from initializing/accessing the same bus.
// No I2C operation occurs while the snapshot mutex is held.
bool read_bytes(uint8_t address, uint8_t reg, uint8_t* out, size_t length) {
  Wire.beginTransmission(address);
  const bool register_written = Wire.write(reg) == 1;
  const uint8_t result = Wire.endTransmission(false);
  // ESP32 Wire defers a repeated START transaction and retains its mutex until
  // requestFrom. Complete that path even when preparing the register failed.
  const size_t received = Wire.requestFrom(address, length, true);
  if (!register_written || result != 0 || received != length) return false;
  for (size_t index = 0; index < length; ++index) {
    const int byte = Wire.read();
    if (byte < 0) return false;
    out[index] = static_cast<uint8_t>(byte);
  }
  return true;
}

void publish(const AcquisitionRecord& record) {
  // Even a reader stalled unexpectedly must not block the acquisition task.
  if (xSemaphoreTake(g_snapshot_mutex, pdMS_TO_TICKS(2)) != pdTRUE) return;
  g_published = record;
  xSemaphoreGive(g_snapshot_mutex);
}

SensorError initialize_imu() {
  uint8_t identity = 0;
  if (!read_bytes(kImuAddress, 0x00, &identity, 1)) return SensorError::ReadFailed;
  if (identity != 0x05) return SensorError::WrongIdentity;
  if (!g_qmi.begin(Wire, kImuAddress, kSdaPin, kSclPin)) {
    return SensorError::ConfigurationFailed;
  }
  // Source: Hiwonder MP3_dance/iic_sensor_task.cpp; same range, ODR, LPF and
  // 25 Hz Madgwick algorithm. SensorLib v0.2.1's FIFTH argument controls the
  // self-test stimulus; explicitly disable it for normal measurements. The
  // vendor's four-argument example otherwise leaves self-test enabled.
  if (g_qmi.configAccelerometer(SensorQMI8658::ACC_RANGE_2G, SensorQMI8658::ACC_ODR_1000Hz,
                                SensorQMI8658::LPF_MODE_0, true, false) != DEV_WIRE_NONE ||
      g_qmi.configGyroscope(SensorQMI8658::GYR_RANGE_256DPS, SensorQMI8658::GYR_ODR_896_8Hz,
                            SensorQMI8658::LPF_MODE_3, true, false) != DEV_WIRE_NONE) {
    return SensorError::ConfigurationFailed;
  }
  // v0.2.1 compares a boolean register-write result with zero in these two
  // methods, reversing their return values. The readback below is authoritative.
  g_qmi.enableGyroscope();
  g_qmi.enableAccelerometer();
  // Driver configuration helpers do not check every register write. Verify the
  // actual range/ODR/self-test, LPF and enable bits before trusting readings.
  uint8_t ctrl2 = 0;
  uint8_t ctrl3 = 0;
  uint8_t ctrl5 = 0;
  uint8_t ctrl7 = 0;
  if (!read_bytes(kImuAddress, 0x03, &ctrl2, 1) || !read_bytes(kImuAddress, 0x04, &ctrl3, 1) ||
      !read_bytes(kImuAddress, 0x06, &ctrl5, 1) || !read_bytes(kImuAddress, 0x08, &ctrl7, 1) ||
      (ctrl2 & 0xBF) != 0x03 || ctrl3 != 0x43 || (ctrl5 & 0x77) != 0x71 || (ctrl7 & 0x83) != 0x03) {
    return SensorError::ConfigurationFailed;
  }
  g_filter.begin(25);
  return SensorError::Calibrating;
}

bool read_imu(IMUdata& acc, IMUdata& gyr, SensorError& error) {
  // v0.2.1 getDataReady() masks an error (-1) into true. Read and validate the
  // status byte directly instead, and require fresh data from both sensors.
  uint8_t status = 0;
  if (!read_bytes(kImuAddress, 0x2E, &status, 1)) {
    error = SensorError::ReadFailed;
    return false;
  }
  if ((status & 0x03) != 0x03) {
    error = SensorError::ReadFailed;
    return false;
  }
  if (!g_qmi.getAccelerometer(acc.x, acc.y, acc.z) || !g_qmi.getGyroscope(gyr.x, gyr.y, gyr.z)) {
    error = SensorError::ReadFailed;
    return false;
  }
  if (!isfinite(acc.x) || !isfinite(acc.y) || !isfinite(acc.z) || !isfinite(gyr.x) ||
      !isfinite(gyr.y) || !isfinite(gyr.z) || (acc.x == 0.0f && acc.y == 0.0f && acc.z == 0.0f)) {
    error = SensorError::InvalidReading;
    return false;
  }
  error = SensorError::None;
  return true;
}

void acquire_sonar(AcquisitionRecord& record) {
  uint8_t bytes[2] = {};
  record.value.dist_valid = false;
  if (!read_bytes(kSonarAddress, 0x00, bytes, sizeof(bytes))) {
    record.value.dist_error = SensorError::ReadFailed;
    return;
  }
  const uint16_t distance_mm =
      static_cast<uint16_t>(bytes[0]) | (static_cast<uint16_t>(bytes[1]) << 8);
  if (distance_mm == UINT16_MAX) {
    // The vendor helper replaces this error sentinel with 500 cm. Do not turn
    // a failed measurement into an apparently clear path.
    record.value.dist_error = SensorError::InvalidReading;
    return;
  }
  record.value.dist_cm = distance_mm / 10.0f;
  record.value.dist_valid = true;
  record.value.dist_error = SensorError::None;
  record.dist_sample_ms = millis();
  record.dist_seen = true;
}

BatteryAdcCalibration initialize_battery() {
  analogReadResolution(12);
  // GPIO34 is ADC1. Characterize once for the exact width and attenuation used
  // by analogRead; eFuse two-point/Vref data take priority over the fallback.
  analogSetPinAttenuation(kBatteryPin, ADC_11db);
  const esp_adc_cal_value_t source = esp_adc_cal_characterize(
      ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, kDefaultAdcVrefMv, &g_battery_adc_chars);
  switch (source) {
    case ESP_ADC_CAL_VAL_EFUSE_TP:
      return BatteryAdcCalibration::EfuseTwoPoint;
    case ESP_ADC_CAL_VAL_EFUSE_VREF:
      return BatteryAdcCalibration::EfuseVref;
    case ESP_ADC_CAL_VAL_DEFAULT_VREF:
      return BatteryAdcCalibration::DefaultVref;
    default:
      return BatteryAdcCalibration::Unavailable;
  }
}

void acquire_battery(AcquisitionRecord& record) {
  const uint16_t raw = analogRead(kBatteryPin);
  record.value.battery_raw = raw;
  record.value.battery_adc_mv = 0;
  record.value.batt_v = 0.0f;
  record.value.batt_valid = false;
  if (record.value.battery_adc_calibration == BatteryAdcCalibration::Unavailable) {
    record.value.batt_error = SensorError::ConfigurationFailed;
    return;
  }
  if (raw == 0 || raw >= 4095) {
    record.value.batt_error = SensorError::InvalidReading;
    return;
  }
  // Convert the very same raw sample; analogReadMilliVolts would read again.
  // This replaces the vendor's fixed raw*3.6 estimate, while external meter
  // verification of the full divider/ADC path remains pending.
  record.value.battery_adc_mv = esp_adc_cal_raw_to_voltage(raw, &g_battery_adc_chars);
  record.value.batt_v = record.value.battery_adc_mv * kBatteryDividerRatio / 1000.0f;
  record.value.batt_valid = true;
  record.value.batt_error = SensorError::None;
  record.batt_sample_ms = millis();
  record.batt_seen = true;
}

void record_timing_fault(AcquisitionRecord& record, SensorTimingFaultReason reason,
                         TickType_t fault_tick, TickType_t cycle_started, TickType_t wake_lateness,
                         const SensorTimingFault& cycle_timing) {
  if (record.value.timing_fault.reason != SensorTimingFaultReason::None) return;
  record.value.timing_fault = cycle_timing;
  record.value.timing_fault.reason = reason;
  record.value.timing_fault.fault_tick_ms = fault_tick * portTICK_PERIOD_MS;
  record.value.timing_fault.wake_late_ms = wake_lateness * portTICK_PERIOD_MS;
  record.value.timing_fault.cycle_elapsed_ms = (fault_tick - cycle_started) * portTICK_PERIOD_MS;
}

void sensor_task(void*) {
  AcquisitionRecord record;
  record.value.task_started = true;
  record.value.imu_error = SensorError::Starting;
  record.value.dist_error = SensorError::Starting;
  record.value.batt_error = SensorError::Starting;
  publish(record);

  record.value.battery_adc_calibration = initialize_battery();
  const bool bus_ready = Wire.begin(kSdaPin, kSclPin, 100000);
  Wire.setTimeOut(10);  // Hardware I2C transaction timeout, milliseconds.
  Wire.setTimeout(2);   // Stream::readBytes used by SensorLib on a short read.
  record.value.imu_error = bus_ready ? initialize_imu() : SensorError::BusInitFailed;
  if (!bus_ready) record.value.dist_error = SensorError::BusInitFailed;
  bool imu_running = record.value.imu_error == SensorError::Calibrating;
  IMUdata gyro_offset = {};
  uint8_t calibration_count = 0;
  const uint32_t calibration_started = millis();
  TickType_t last_wake = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(kSamplePeriodMs);

  for (;;) {
    const TickType_t cycle_started = xTaskGetTickCount();
    const TickType_t wake_lateness = cycle_started - last_wake;
    SensorTimingFault cycle_timing;
    if (imu_running && calibration_count >= kCalibrationSamples && wake_lateness >= period) {
      // No sensor stage has run before this decision. Later sonar/ADC work in
      // this cycle must not overwrite the first fault's empty measured mask.
      record_timing_fault(record, SensorTimingFaultReason::WakeLate, cycle_started, cycle_started,
                          wake_lateness, cycle_timing);
      record.value.imu_valid = false;
      record.value.imu_error = SensorError::TimingFault;
      imu_running = false;
    }
    if (imu_running) {
      const int64_t imu_started_us = esp_timer_get_time();
      IMUdata acc = {};
      IMUdata gyr = {};
      SensorError read_error = SensorError::None;
      if (read_imu(acc, gyr, read_error)) {
        record.value.accel_g[0] = acc.x;
        record.value.accel_g[1] = acc.y;
        record.value.accel_g[2] = acc.z;
        record.value.gyro_dps[0] = gyr.x;
        record.value.gyro_dps[1] = gyr.y;
        record.value.gyro_dps[2] = gyr.z;
        if (calibration_count < kCalibrationSamples) {
          // Same first sample + ten pairwise averages as the vendor example.
          // The body must remain still at startup; no movement/zeroing occurs here.
          if (calibration_count == 0) {
            gyro_offset = gyr;
          } else {
            gyro_offset.x = (gyro_offset.x + gyr.x) / 2.0f;
            gyro_offset.y = (gyro_offset.y + gyr.y) / 2.0f;
            gyro_offset.z = (gyro_offset.z + gyr.z) / 2.0f;
          }
          ++calibration_count;
        } else {
          g_filter.updateIMU(gyr.x - gyro_offset.x, gyr.y - gyro_offset.y, gyr.z - gyro_offset.z,
                             acc.x, acc.y, acc.z);
          record.value.roll = g_filter.getRoll();
          record.value.pitch = g_filter.getPitch();
          record.value.yaw = fmodf(g_filter.getYaw(), 360.0f);
          if (record.value.yaw < 0.0f) record.value.yaw += 360.0f;
          record.value.imu_valid = isfinite(record.value.roll) && isfinite(record.value.pitch) &&
                                   isfinite(record.value.yaw);
          record.value.imu_error =
              record.value.imu_valid ? SensorError::None : SensorError::InvalidReading;
          if (record.value.imu_valid) {
            record.imu_sample_ms = millis();
            record.imu_seen = true;
          } else {
            imu_running = false;
          }
        }
      } else if (calibration_count >= kCalibrationSamples) {
        // Diagnostic mode: a missing integration sample invalidates the filter
        // until reboot. Do not bridge unknown motion or silently re-zero yaw.
        record.value.imu_valid = false;
        record.value.imu_error = read_error;
        imu_running = false;
      }
      if (calibration_count < kCalibrationSamples &&
          millis() - calibration_started >= kCalibrationTimeoutMs) {
        record.value.imu_error = SensorError::ReadFailed;
        imu_running = false;
      }
      cycle_timing.imu_filter_us = esp_timer_get_time() - imu_started_us;
      cycle_timing.stages_measured_mask |= kSensorTimingStageImuFilter;
    }
    if (bus_ready) {
      const int64_t sonar_started_us = esp_timer_get_time();
      acquire_sonar(record);
      cycle_timing.sonar_us = esp_timer_get_time() - sonar_started_us;
      cycle_timing.stages_measured_mask |= kSensorTimingStageSonar;
    }
    const int64_t adc_started_us = esp_timer_get_time();
    acquire_battery(record);
    cycle_timing.adc_us = esp_timer_get_time() - adc_started_us;
    cycle_timing.stages_measured_mask |= kSensorTimingStageAdc;

    // Deadline scheduling keeps the filter at 25 Hz without an accumulating
    // read-time delay. An entire missed period cannot be hidden by catch-up
    // updates with invented samples; this diagnostic build latches that fault.
    // Preserve the original short-circuit order and separate tick reads: the
    // scheduled-elapsed clock is read only if cycle-elapsed did not fire.
    TickType_t fault_check_tick = xTaskGetTickCount();
    SensorTimingFaultReason timing_reason = SensorTimingFaultReason::None;
    if (fault_check_tick - cycle_started >= period) {
      timing_reason = SensorTimingFaultReason::CycleElapsed;
    } else {
      fault_check_tick = xTaskGetTickCount();
      if (fault_check_tick - last_wake >= 2 * period) {
        timing_reason = SensorTimingFaultReason::ScheduledElapsed;
      }
    }
    if (timing_reason != SensorTimingFaultReason::None) {
      if (imu_running && calibration_count >= kCalibrationSamples) {
        record_timing_fault(record, timing_reason, fault_check_tick, cycle_started, wake_lateness,
                            cycle_timing);
        record.value.imu_valid = false;
        record.value.imu_error = SensorError::TimingFault;
        imu_running = false;
      }
      last_wake = xTaskGetTickCount();
    }
    publish(record);
    vTaskDelayUntil(&last_wake, period);
  }
}

void update_age(uint32_t now, uint32_t sampled, bool seen, uint32_t& age, bool& valid,
                SensorError& error) {
  if (!seen) {
    age = UINT32_MAX;
    valid = false;
    return;
  }
  // The task may publish one tick newer than the caller's captured `now`.
  // Unsigned subtraction otherwise turns that fresh sample into 49 days old.
  const uint32_t elapsed = now - sampled;
  age = sampled - now <= kSamplePeriodMs ? 0 : elapsed;
  if (valid && age > kMaxAgeMs) {
    valid = false;
    error = SensorError::Stale;
  }
}

}  // namespace
#endif

bool SensorHal::begin() {
#if MECHADOG_ENABLE_SENSORS
  if (g_sensor_task != nullptr) return true;
  if (g_snapshot_mutex == nullptr) g_snapshot_mutex = xSemaphoreCreateMutex();
  if (g_snapshot_mutex == nullptr) {
    g_start_error = SensorError::TaskCreationFailed;
    return false;
  }
  if (xTaskCreatePinnedToCore(sensor_task, "MechDogSensors", 4096, nullptr, 3, &g_sensor_task, 0) !=
      pdPASS) {
    g_sensor_task = nullptr;
    g_start_error = SensorError::TaskCreationFailed;
    return false;
  }
  return true;
#else
  return false;
#endif
}

SensorSnapshot SensorHal::snapshot(uint32_t now_ms) const {
#if MECHADOG_ENABLE_SENSORS
  SensorSnapshot unavailable;
  if (g_sensor_task == nullptr) {
    unavailable.imu_error = g_start_error;
    unavailable.dist_error = g_start_error;
    unavailable.batt_error = g_start_error;
    return unavailable;
  }
  if (xSemaphoreTake(g_snapshot_mutex, 0) != pdTRUE) {
    unavailable.task_started = true;
    unavailable.imu_error = SensorError::SnapshotBusy;
    unavailable.dist_error = SensorError::SnapshotBusy;
    unavailable.batt_error = SensorError::SnapshotBusy;
    return unavailable;
  }
  AcquisitionRecord record = g_published;
  xSemaphoreGive(g_snapshot_mutex);
  update_age(now_ms, record.imu_sample_ms, record.imu_seen, record.value.imu_age_ms,
             record.value.imu_valid, record.value.imu_error);
  update_age(now_ms, record.dist_sample_ms, record.dist_seen, record.value.dist_age_ms,
             record.value.dist_valid, record.value.dist_error);
  update_age(now_ms, record.batt_sample_ms, record.batt_seen, record.value.batt_age_ms,
             record.value.batt_valid, record.value.batt_error);
  return record.value;
#else
  (void)now_ms;
  return SensorSnapshot{};
#endif
}

}  // namespace mechadog
