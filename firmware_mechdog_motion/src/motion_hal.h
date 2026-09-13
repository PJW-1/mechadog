#ifndef MECHADOG_MOTION_HAL_H
#define MECHADOG_MOTION_HAL_H

#ifndef MECHADOG_ENABLE_ACTUATORS
#define MECHADOG_ENABLE_ACTUATORS 0
#endif

namespace mechadog {

// Runtime gate in front of the servo driver. The compile flag decides whether
// the driver is linked at all; this decides whether it may drive right now.
// A build without actuator support never reports enabled — a request cannot
// conjure hardware that is not linked in.
bool actuatorsRuntimeEnabled();
void actuatorsSetRuntime(bool enabled);

// Keeps network and safety logic testable without energizing the servos.
// The actuator build uses Hiwonder's HW_MechDog implementation.
class MotionHal {
 public:
  bool begin();
  void move(float step_mm, float angle_deg);
  void stop();
  bool actuators_enabled() const;
};

}  // namespace mechadog

#endif  // MECHADOG_MOTION_HAL_H
