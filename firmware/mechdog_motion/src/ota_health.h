#ifndef MECHADOG_OTA_HEALTH_H
#define MECHADOG_OTA_HEALTH_H
#include <stdint.h>
namespace mechadog {
// A contended nonblocking snapshot is unknown, not a new bad measurement.
// Keep the previous observation ONLY until that actual sample expires.
class OtaHealth {
 public:
  bool observe(uint32_t now, bool network, bool valid, bool busy, uint32_t remaining_ms) {
    if (valid) {
      have_sample_ = true;
      expires_ = now + remaining_ms;
    } else if (!busy) {
      have_sample_ = false;
    }
    const bool usable = network && have_sample_ && static_cast<int32_t>(now - expires_) <= 0;
    if (!usable) {
      tracking_ = false;
      return false;
    }
    if (!tracking_) {
      since_ = now;
      tracking_ = true;
    }
    return now - since_ >= 5000;
  }

 private:
  bool have_sample_ = false;
  bool tracking_ = false;
  uint32_t expires_ = 0;
  uint32_t since_ = 0;
};
}  // namespace mechadog
#endif
