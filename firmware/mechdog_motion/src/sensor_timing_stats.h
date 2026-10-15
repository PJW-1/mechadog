// Fixed-memory wall-time distributions for the stationary sensor A/B test.
#ifndef MECHADOG_SENSOR_TIMING_STATS_H
#define MECHADOG_SENSOR_TIMING_STATS_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

namespace mechadog {

// Inclusive upper bounds in microseconds. The final counter is overflow.
// Zero has its own bin so on-time wakeups are not reported as a 100 us delay.
constexpr uint32_t kSensorTimingUpperBoundsUs[] = {0,     100,    250,    500,    1000,  2000,
                                                   4000,  8000,   12000,  20000,  30000, 40000,
                                                   50000, 100000, 250000, 1000000};
constexpr size_t kSensorTimingFiniteBins =
    sizeof(kSensorTimingUpperBoundsUs) / sizeof(kSensorTimingUpperBoundsUs[0]);
constexpr size_t kSensorTimingBins = kSensorTimingFiniteBins + 1;

struct SensorElapsedDistribution {
  uint32_t count = 0;
  uint64_t sum_us = 0;
  uint64_t max_us = 0;
  uint32_t histogram[kSensorTimingBins] = {};
  bool saturated = false;

  // Bounded work and no allocation. Saturation is diagnostic only: no sensor
  // read, deadline or safety decision depends on these counters.
  void observe(uint64_t elapsed_us) {
    if (count == UINT32_MAX) {
      saturated = true;
      return;
    }
    ++count;
    if (UINT64_MAX - sum_us < elapsed_us) {
      sum_us = UINT64_MAX;
      saturated = true;
    } else {
      sum_us += elapsed_us;
    }
    if (elapsed_us > max_us) max_us = elapsed_us;
    size_t bin = 0;
    while (bin < kSensorTimingFiniteBins && elapsed_us > kSensorTimingUpperBoundsUs[bin]) ++bin;
    ++histogram[bin];
  }

  // Nearest-rank percentile, estimated by the containing bin's upper bound.
  // Overflow uses the observed maximum; neither is an exact percentile.
  // count==0 means unavailable, even though the numeric return value is zero.
  uint64_t percentile_upper_us(uint8_t percentile) const {
    if (count == 0 || percentile == 0 || percentile > 100) return 0;
    const uint64_t rank = (static_cast<uint64_t>(count) * percentile + 99) / 100;
    uint64_t cumulative = 0;
    for (size_t bin = 0; bin < kSensorTimingBins; ++bin) {
      cumulative += histogram[bin];
      if (cumulative >= rank) {
        if (bin == kSensorTimingFiniteBins) return max_us;
        const uint64_t upper = kSensorTimingUpperBoundsUs[bin];
        return upper < max_us ? upper : max_us;
      }
    }
    return max_us;
  }
};

struct SensorPerformanceSnapshot {
  // Cumulative since this boot, including calibration and failed attempts.
  // An unexecuted stage is omitted, never recorded as a zero-duration sample.
  SensorElapsedDistribution cycle;
  SensorElapsedDistribution wake;
  SensorElapsedDistribution imu;
  SensorElapsedDistribution sonar;
  SensorElapsedDistribution adc;
  uint64_t captured_us = 0;
  uint32_t publish_dropped = 0;
  uint32_t stack_min_free_bytes = 0;  // ESP-IDF FreeRTOS uses bytes here.
  int8_t execution_core = -1;
};

// One metric per line, rotated at 1 Hz by the caller. Even all maximum-width
// counters fit below 640 bytes; with the 128-byte UART reserve this fits the
// existing 1024-byte TX ring. Raw bins allow host-side interval differencing.
constexpr size_t kSensorPerformanceLineBytes = 640;

inline int format_sensor_performance_line(char* out, size_t capacity,
                                          const SensorPerformanceSnapshot& snapshot,
                                          const SensorElapsedDistribution& distribution,
                                          const char* metric, uint8_t configured_core,
                                          uint32_t log_dropped) {
  int length = snprintf(
      out, capacity,
      "Perf: v=1 at_us=%llu core=%d cfg=%u stack_b=%lu drop=%lu pub_drop=%lu metric=%s "
      "n=%lu sum_us=%llu max_us=%llu p95_ub_us=%llu p99_ub_us=%llu sat=%u h=",
      static_cast<unsigned long long>(snapshot.captured_us),
      static_cast<int>(snapshot.execution_core), static_cast<unsigned>(configured_core),
      static_cast<unsigned long>(snapshot.stack_min_free_bytes),
      static_cast<unsigned long>(log_dropped), static_cast<unsigned long>(snapshot.publish_dropped),
      metric, static_cast<unsigned long>(distribution.count),
      static_cast<unsigned long long>(distribution.sum_us),
      static_cast<unsigned long long>(distribution.max_us),
      static_cast<unsigned long long>(distribution.percentile_upper_us(95)),
      static_cast<unsigned long long>(distribution.percentile_upper_us(99)),
      static_cast<unsigned>(distribution.saturated));
  if (length <= 0 || static_cast<size_t>(length) >= capacity) return -1;
  for (size_t bin = 0; bin < kSensorTimingBins; ++bin) {
    const int added = snprintf(out + length, capacity - static_cast<size_t>(length), "%lu%c",
                               static_cast<unsigned long>(distribution.histogram[bin]),
                               bin + 1 == kSensorTimingBins ? '\n' : ',');
    if (added <= 0 || static_cast<size_t>(added) >= capacity - static_cast<size_t>(length)) {
      return -1;
    }
    length += added;
  }
  return length;
}

}  // namespace mechadog

#endif  // MECHADOG_SENSOR_TIMING_STATS_H
