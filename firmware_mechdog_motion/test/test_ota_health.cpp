#include <cstdlib>
#include <iostream>

#include "ota_health.h"

static void check(bool value) {
  if (!value) std::abort();
}

int main() {
  mechadog::OtaHealth health;
  check(!health.observe(0, true, false, true, 0));
  for (uint32_t ms = 0; ms < 5000; ms += 100) check(!health.observe(ms, true, true, false, 200));
  check(health.observe(5000, true, true, false, 200));
  check(health.observe(5100, true, false, true, 0));
  check(health.observe(5200, true, false, true, 0));
  check(!health.observe(5201, true, false, true, 0));
  check(!health.observe(5300, true, true, false, 200));
  for (uint32_t ms = 5400; ms <= 10300; ms += 100)
    check(health.observe(ms, true, true, false, 200) == (ms == 10300));
  check(!health.observe(10301, true, false, false, 0));
  check(!health.observe(10302, true, false, true, 0));
  for (uint32_t ms = 10400; ms <= 15400; ms += 100)
    check(health.observe(ms, true, true, false, 200) == (ms == 15400));
  check(!health.observe(15401, false, true, false, 200));
  check(!health.observe(15402, true, true, false, 200));
  mechadog::OtaHealth wrap;
  const uint32_t start = UINT32_MAX - 2500;
  for (uint32_t delta = 0; delta <= 5000; delta += 100)
    check(wrap.observe(start + delta, true, true, false, 100) == (delta == 5000));
  check(wrap.observe(start + 5100, true, false, true, 0));
  check(!wrap.observe(start + 5101, true, false, true, 0));
  std::cout << "OTA health: startup, 5s window, snapshot contention, expiry, invalid sensor, "
               "network loss and millisecond wrap passed\n";
}
