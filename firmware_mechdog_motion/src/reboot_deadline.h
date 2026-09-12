#ifndef MECHADOG_REBOOT_DEADLINE_H
#define MECHADOG_REBOOT_DEADLINE_H

#include <stdint.h>

#include <atomic>

namespace mechadog {
// The HTTPS task schedules a restart while the loop task polls it. Publish the
// deadline before arming; zero is a valid deadline after millis() wraps.
// Delays and the interval between polls must remain below 2^31 milliseconds.
// There is no cancellation: once due, the caller restarts the device.
class RebootDeadline {
 public:
  void schedule(uint32_t now_ms, uint32_t delay_ms) {
    deadline_ms_.store(now_ms + delay_ms, std::memory_order_relaxed);
    armed_.store(true, std::memory_order_release);
  }

  bool due(uint32_t now_ms) const {
    if (!armed_.load(std::memory_order_acquire)) return false;
    return now_ms - deadline_ms_.load(std::memory_order_relaxed) < UINT32_C(0x80000000);
  }

 private:
  std::atomic<uint32_t> deadline_ms_{0};
  std::atomic<bool> armed_{false};
};
}  // namespace mechadog

#endif
