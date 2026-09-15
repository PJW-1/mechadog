#include <cstdio>
#include <cstdlib>

#include "motion_safety_state.h"

namespace {
int checks = 0;

void check(bool condition) {
  ++checks;
  if (!condition) {
    std::fprintf(stderr, "Motion safety state check %d failed\n", checks);
    std::abort();
  }
}
}  // namespace

int main() {
  mechadog::MotionSafetyState state;
  check(state.safe_latched && !state.walking && !state.service_mode);
  check(!state.can_move() && state.can_action());

  state.reset_safe();
  check(!state.safe_latched && state.can_move());
  state.note_move(60.0F, 0.0F);
  check(state.walking && !state.can_action());

  // Link loss or ESTOP parks software state as well as the motors, so the
  // failsafe posture ACTION remains available.
  state.latch();
  check(state.safe_latched && !state.walking && state.can_action());

  state.reset_safe();
  state.note_move(0.0F, 20.0F);
  state.enter_service();
  check(state.safe_latched && !state.walking && state.service_mode);
  check(!state.can_move() && state.can_action());

  // RESET_SAFE may be acknowledged during SERVICE, but leaving SERVICE must
  // re-latch. The operator then performs a fresh post-maintenance reset.
  state.reset_safe();
  check(!state.safe_latched && state.service_mode && !state.can_move());
  state.exit_service();
  check(state.safe_latched && !state.walking && !state.service_mode);
  check(!state.can_move() && state.can_action());

  state.reset_safe();
  state.note_move(0.0F, 0.0F);
  check(!state.walking && state.can_action());
  state.stop();
  check(!state.walking);

  std::printf("Motion safety state: %d assertions passed\n", checks);
}
