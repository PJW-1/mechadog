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
  // 래치 중에도 참이다 — FAILSAFE 안정 자세(엎드림)가 ACTION 경로로 온다.
  // SERVICE 는 다르다. **그 모드에서만 루프 워치독이 무장하는데**(750ms) 벤더
  // `action_run` 은 실측 1,003~1,006ms 를 blocking 한다. 받아주면 마감을 넘겨
  // 감시 태스크가 esp_restart() 를 부른다 — 정비하려고 세워 둔 기체가 다리를
  // 움직이다 리셋된다. 부팅 무장을 막아 둔 것과 같은 이유로 여기서도 막는다.
  bool can_action() const { return !walking && !service_mode; }

  void note_move(float step_mm, float angle_deg) { walking = step_mm != 0.0F || angle_deg != 0.0F; }
};

}  // namespace mechadog

#endif  // MECHADOG_MOTION_SAFETY_STATE_H
