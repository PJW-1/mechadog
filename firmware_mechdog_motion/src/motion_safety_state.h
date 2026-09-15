#ifndef MECHADOG_MOTION_SAFETY_STATE_H
#define MECHADOG_MOTION_SAFETY_STATE_H

namespace mechadog {

// Hardware-independent state for safety decisions that must stay consistent
// across STOP, FAILSAFE and SERVICE transitions.
struct MotionSafetyState {
  bool walking = false;
  bool safe_latched = true;
  bool service_mode = false;

  void stop() { walking = false; }

  void latch() {
    walking = false;
    safe_latched = true;
  }

  void reset_safe() {
    walking = false;
    safe_latched = false;
  }

  void enter_service() {
    walking = false;
    safe_latched = true;
    service_mode = true;
  }

  void exit_service() {
    walking = false;
    safe_latched = true;
    service_mode = false;
  }

  bool can_move() const { return !safe_latched && !service_mode; }
  bool can_action() const { return !walking; }

  void note_move(float step_mm, float angle_deg) { walking = step_mm != 0.0F || angle_deg != 0.0F; }
};

}  // namespace mechadog

#endif  // MECHADOG_MOTION_SAFETY_STATE_H
