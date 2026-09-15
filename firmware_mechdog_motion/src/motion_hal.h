#ifndef MECHADOG_MOTION_HAL_H
#define MECHADOG_MOTION_HAL_H

#ifndef MECHADOG_ENABLE_ACTUATORS
#define MECHADOG_ENABLE_ACTUATORS 0
#endif

namespace mechadog {

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
