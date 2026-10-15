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

  // Body attitude. `yaw` is intentionally absent: both leg joints lie in the
  // fore-aft plane, so body yaw is geometrically impossible (DR-11).
  void pose(float pitch_deg, float roll_deg, float height_mm, int duration_ms);

  // Runs a built-in action by protocol id. Returns false for unmapped ids.
  //
  // WARNING: the vendor blocks with delay() per action segment, so loop() stops
  // for the whole duration. Callers must refuse this while walking; only
  // single-segment (1000 ms) actions are mapped for the same reason.
  //
  // The vendor takes a name, not a number, and silently does nothing when the
  // name is unknown - a typo would become a "quiet no-op" nobody can diagnose.
  // So the id -> name table lives here and a host test pins it to config.yaml.
  bool action(int id);
};

// Protocol ACTION id -> vendor action name.
//
// WARNING: keep this identical to `actions.id_map` in config/config.yaml.
// `tests/test_firmware_actions.py` compares the two and fails CI when they
// drift - the wire carries a number but the vendor resolves a name, so a silent
// mismatch would run the wrong motion.
struct ActionName {
  int id;
  const char* name;
};
extern const ActionName kActionNames[];
extern const int kActionNameCount;

}  // namespace mechadog

#endif  // MECHADOG_MOTION_HAL_H
