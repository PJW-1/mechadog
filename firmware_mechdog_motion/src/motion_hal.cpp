#include "motion_hal.h"

#if MECHADOG_ENABLE_ACTUATORS
#include "HW_MechDog.h"

namespace {
MechDog g_mechdog;
}
#endif

namespace mechadog {

bool MotionHal::begin() {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.MechDog_init();
  g_mechdog.move(0, 0);
#endif
  return true;
}

void MotionHal::move(float step_mm, float angle_deg) {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.move(step_mm, angle_deg);
#else
  (void)step_mm;
  (void)angle_deg;
#endif
}

void MotionHal::stop() {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.move(0, 0);
#endif
}

bool MotionHal::actuators_enabled() const {
  return MECHADOG_ENABLE_ACTUATORS != 0;
}

}  // namespace mechadog
