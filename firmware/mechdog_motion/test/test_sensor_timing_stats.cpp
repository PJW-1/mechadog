#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sensor_timing_stats.h"

static void check(bool value) {
  if (!value) abort();
}

int main() {
  mechadog::SensorElapsedDistribution empty;
  check(empty.count == 0 && empty.percentile_upper_us(95) == 0);
  empty.observe(0);
  check(empty.count == 1 && empty.histogram[0] == 1 && empty.percentile_upper_us(99) == 0);

  // Inclusive boundaries and their next microsecond must not share a bin.
  for (size_t bin = 0; bin < mechadog::kSensorTimingFiniteBins; ++bin) {
    mechadog::SensorElapsedDistribution edge;
    const uint64_t upper = mechadog::kSensorTimingUpperBoundsUs[bin];
    edge.observe(upper);
    edge.observe(upper + 1);
    check(edge.histogram[bin] == 1 && edge.histogram[bin + 1] == 1);
    check(edge.count == 2 && edge.sum_us == 2 * upper + 1 && edge.max_us == upper + 1);
  }

  // Exact 95/99 ranks, overflow-bin upper bound, and the observed-max clamp.
  mechadog::SensorElapsedDistribution ranks;
  for (unsigned index = 0; index < 94; ++index) ranks.observe(0);
  ranks.observe(1001);
  for (unsigned index = 0; index < 3; ++index) ranks.observe(40000);
  ranks.observe(1000001);
  ranks.observe(5920000);
  check(ranks.count == 100 && ranks.sum_us == 7041002 && ranks.max_us == 5920000);
  check(ranks.percentile_upper_us(95) == 2000);
  check(ranks.percentile_upper_us(99) == 5920000);
  check(ranks.percentile_upper_us(0) == 0 && ranks.percentile_upper_us(101) == 0);
  mechadog::SensorElapsedDistribution one;
  one.observe(150);
  check(one.percentile_upper_us(95) == 150);

  // Large counters must neither wrap nor change the histogram/count relation.
  mechadog::SensorElapsedDistribution saturated;
  saturated.count = UINT32_MAX - 1;
  saturated.histogram[0] = UINT32_MAX - 1;
  saturated.observe(0);
  saturated.observe(1);
  check(saturated.count == UINT32_MAX && saturated.histogram[0] == UINT32_MAX);
  check(saturated.histogram[1] == 0 && saturated.saturated);
  mechadog::SensorElapsedDistribution sum_overflow;
  sum_overflow.observe(UINT64_MAX);
  sum_overflow.observe(1);
  check(sum_overflow.count == 2 && sum_overflow.sum_us == UINT64_MAX && sum_overflow.saturated);
  check(sum_overflow.histogram[mechadog::kSensorTimingFiniteBins] == 1);

  // Host analysis can subtract cumulative bins and sums across a dropped UART
  // line. Unexecuted stages keep count=0 rather than inventing zero-cost work.
  mechadog::SensorPerformanceSnapshot snapshot;
  snapshot.cycle.observe(4000);
  const mechadog::SensorElapsedDistribution before = snapshot.cycle;
  snapshot.cycle.observe(4001);
  snapshot.cycle.observe(592000);
  check(snapshot.cycle.count - before.count == 2);
  check(snapshot.cycle.sum_us - before.sum_us == 596001);
  check(snapshot.cycle.histogram[7] - before.histogram[7] == 1);
  check(snapshot.cycle.histogram[15] - before.histogram[15] == 1);
  check(snapshot.imu.count == 0);

  // Exercise maximum field widths, including all bins, to prove the whole
  // diagnostic line plus FIFO reserve fits the unchanged 1024-byte TX buffer.
  snapshot.captured_us = UINT64_MAX;
  snapshot.stack_min_free_bytes = UINT32_MAX;
  snapshot.publish_dropped = UINT32_MAX;
  snapshot.execution_core = 1;
  snapshot.cycle.count = UINT32_MAX;
  snapshot.cycle.sum_us = UINT64_MAX;
  snapshot.cycle.max_us = UINT64_MAX;
  for (size_t bin = 0; bin < mechadog::kSensorTimingBins; ++bin) {
    snapshot.cycle.histogram[bin] = UINT32_MAX;
  }
  char line[mechadog::kSensorPerformanceLineBytes];
  const int length = mechadog::format_sensor_performance_line(
      line, sizeof(line), snapshot, snapshot.cycle, "cycle", 1, UINT32_MAX);
  check(length > 0 && length + 128 < 1024);
  check(line[length - 1] == '\n' && line[length] == '\0');
  check(strstr(line, "Perf: v=1 ") == line);
  check(strstr(line, " core=1 cfg=1 ") != nullptr);
  // All-bin widths above put both percentile ranks in bin zero. Independently
  // exercise 20-digit percentile bounds in the overflow bin and core=-1.
  memset(snapshot.cycle.histogram, 0, sizeof(snapshot.cycle.histogram));
  snapshot.cycle.histogram[mechadog::kSensorTimingFiniteBins] = UINT32_MAX;
  snapshot.execution_core = -1;
  const int overflow_length = mechadog::format_sensor_performance_line(
      line, sizeof(line), snapshot, snapshot.cycle, "cycle", 1, UINT32_MAX);
  check(overflow_length > 0 && overflow_length + 128 < 1024);
  check(strstr(line, "p95_ub_us=18446744073709551615 p99_ub_us=18446744073709551615") != nullptr);
  check(strstr(line, " core=-1 ") != nullptr);
  char short_line[8];
  check(mechadog::format_sensor_performance_line(short_line, sizeof(short_line), snapshot,
                                                 snapshot.cycle, "cycle", 1, 0) == -1);

  printf(
      "Sensor timing: histogram boundaries, percentile ranks, overflow, cumulative "
      "intervals and bounded UART formatting passed; all-bin line=%d, overflow line=%d bytes\n",
      length, overflow_length);
}
