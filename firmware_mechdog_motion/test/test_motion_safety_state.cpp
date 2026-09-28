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
  // SERVICE 는 ACTION 도 막는다. 이 모드에서만 루프 워치독이 무장하고(750ms)
  // 벤더 action_run 은 1초 남짓 blocking 이라, 받아주면 정비 중 보드가 재부팅된다.
  check(!state.can_move() && !state.can_action());

  // RESET_SAFE may be acknowledged during SERVICE, but leaving SERVICE must
  // re-latch. The operator then performs a fresh post-maintenance reset.
  state.reset_safe();
  check(!state.safe_latched && state.service_mode && !state.can_move());
  // 래치를 풀어도 SERVICE 인 동안에는 ACTION 이 다시 열리지 않는다.
  check(!state.can_action());
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
