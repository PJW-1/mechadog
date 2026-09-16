#include "safety_monitor.h"

namespace mechadog {

bool SafetyMonitor::fresh_sample(bool valid, uint32_t age_ms, uint32_t now_ms,
                                 uint32_t* last_sample_ms) const {
  if (!valid || age_ms > thresholds_.max_sample_age_ms) return false;
  // 측정 시각으로 되돌린다. 같은 표본을 다시 받으면 시각이 같으므로 세지 않는다.
  const uint32_t sample_ms = now_ms - age_ms;
  if (*last_sample_ms != UINT32_MAX && sample_ms == *last_sample_ms) return false;
  *last_sample_ms = sample_ms;
  return true;
}

SafetyVerdict SafetyMonitor::update(const SafetyReading& reading) {
  SafetyVerdict verdict;

  // ── 근거리 반사 정지 (3.2.6) ──────────────────────────────────────────
  if (fresh_sample(reading.dist_valid, reading.dist_age_ms, reading.now_ms,
                   &last_dist_sample_ms_)) {
    decision_age_ms_ = reading.dist_age_ms;
    if (reading.dist_cm < thresholds_.obstacle_stop_cm) {
      clear_count_ = 0;
      if (near_count_ < thresholds_.obstacle_samples) ++near_count_;
      if (!obstacle_ && near_count_ >= thresholds_.obstacle_samples) {
        obstacle_ = true;
        verdict.obstacle_started = true;
      }
    } else if (reading.dist_cm >= thresholds_.obstacle_clear_cm) {
      near_count_ = 0;
      if (clear_count_ < thresholds_.clear_samples) ++clear_count_;
      if (obstacle_ && clear_count_ >= thresholds_.clear_samples) obstacle_ = false;
    } else {
      // 임계와 해제 사이 — 어느 쪽으로도 세지 않고 현재 상태를 유지한다.
      near_count_ = 0;
      clear_count_ = 0;
    }
  }
  // 새 표본이 없으면(같은 표본의 재방문 · 무효 · 너무 오래된 값) **아무것도 하지
  // 않는다.** ⚠️ 여기서 카운터를 지우면 `loop()` 가 1ms 마다 도는 동안 40ms 마다
  // 오는 표본의 누적이 매번 지워져 **연속 판정이 영원히 성립하지 않는다.**
  // 걸린 것을 푸는 것도 하지 않는다 — 장애물이 그대로 있는데 전진이 열리는 쪽이
  // 위험하고, 걸린 채로 두어도 후진은 살아 있어 빠져나갈 길이 있다.
  verdict.obstacle = obstacle_;
  verdict.decision_age_ms = decision_age_ms_;

  // ── 저전압 감시 (3.2.2) ───────────────────────────────────────────────
  // 경고는 표본 하나로 켠다 — 표시일 뿐 동작을 막지 않는다.
  lowbatt_ = reading.batt_valid && reading.batt_v <= thresholds_.battery_warn_v;
  verdict.lowbatt = lowbatt_;

  if (fresh_sample(reading.batt_valid, reading.batt_age_ms, reading.now_ms,
                   &last_batt_sample_ms_)) {
    if (reading.batt_v <= thresholds_.battery_shutdown_v) {
      if (low_count_ < thresholds_.shutdown_samples) ++low_count_;
      if (!shutdown_reported_ && low_count_ >= thresholds_.shutdown_samples) {
        shutdown_reported_ = true;
        verdict.shutdown = true;
      }
    } else if (reading.batt_v > thresholds_.battery_warn_v) {
      // 경고선 위로 회복됐을 때만 다시 무장한다. 셧다운선 바로 위에서 떨었다고
      // 재무장하면 같은 전압대에서 래치가 반복해서 걸린다.
      low_count_ = 0;
      shutdown_reported_ = false;
    } else {
      low_count_ = 0;
    }
  }

  return verdict;
}

}  // namespace mechadog
