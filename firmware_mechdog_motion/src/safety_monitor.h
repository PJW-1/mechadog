#ifndef MECHADOG_SAFETY_MONITOR_H
#define MECHADOG_SAFETY_MONITOR_H

#include <stdint.h>

namespace mechadog {

// Tier 1 온보드 안전 판정 — 근거리 반사 정지(`3.2.6`)와 저전압 감시(`3.2.2`).
//
// ⚠️ **호스트를 기다리지 않는다** (아키텍처 1.2). 10Hz 명령 격자만으로도 평균
// 50ms 를 쓰고, 호스트가 죽으면 판정 자체가 사라진다.
//
// 이 파일에는 하드웨어 호출이 없다 — 센서 값을 받아 판정만 돌려준다. 그래서
// 로봇 없이 PC 에서 전수 시험할 수 있다 (CONTRIBUTING 5.2 HAL 분리).
struct SafetyThresholds {
  // config/config.yaml `safety` 절과 같은 값이어야 한다.
  float obstacle_stop_cm = 25.0F;   // safety.obstacle_stop_cm (FR-2.2)
  float battery_warn_v = 7.0F;      // safety.battery_warn_v (NFR-2.3)
  float battery_shutdown_v = 6.6F;  // safety.battery_shutdown_v (NFR-2.3)

  // ⚠️ **해제는 더 먼 거리에서 한다.** 같은 값으로 걸고 풀면 임계 부근에서
  // 초당 수십 번 진동하고, 그때마다 호스트가 회피 시퀀스를 열었다 닫는다.
  float obstacle_clear_cm = 30.0F;

  // 단발 이상값으로 멈추지 않는다 (`3.2.6` DoD ④). 센서 주기가 40ms 이므로
  // 2표본 = 약 80ms 다. 실측에서 초음파는 기준보다 5~8% 짧게 읽으므로(`2.1.3`)
  // 임계를 넘겼다는 판정 자체는 안전측이다.
  uint8_t obstacle_samples = 2;
  uint8_t clear_samples = 2;

  // 보행 중에는 전압이 순간적으로 내려간다. 한 표본으로 세우면 걷다가 멈춘다.
  uint8_t shutdown_samples = 3;

  // 이보다 오래된 값으로는 **새 판정을 만들지 않는다.** 센서가 죽은 뒤에도
  // 마지막 값으로 계속 판정하면 그것은 측정이 아니라 기억이다.
  uint32_t max_sample_age_ms = 200;
};

// 센서 한 번의 관측. `*_age_ms` 는 그 값이 언제 측정됐는지이며, 같은 표본을
// 여러 번 넘겨도 판정이 앞으로 가지 않게 하는 근거로도 쓴다 — `loop()` 는
// 1ms 마다 도는데 센서는 40ms 마다 갱신된다.
struct SafetyReading {
  uint32_t now_ms = 0;
  bool dist_valid = false;
  float dist_cm = 0.0F;
  uint32_t dist_age_ms = UINT32_MAX;
  bool batt_valid = false;
  float batt_v = 0.0F;
  uint32_t batt_age_ms = UINT32_MAX;
};

struct SafetyVerdict {
  bool obstacle = false;          // flags.obstacle — 지금 반사 정지가 걸려 있는가
  bool obstacle_started = false;  // 이번 갱신에서 걸렸다 → move(0,0) 을 한 번 부른다
  bool lowbatt = false;           // flags.lowbatt
  bool shutdown = false;          // 이번 갱신에서 셧다운 임계를 넘겼다 → 래치
  // 판정에 쓴 거리 표본의 나이(ms). `3.2.6` DoD ① 의 «감지 → 정지» 를 온보드
  // 시각으로 입증할 때 쓴다.
  uint32_t decision_age_ms = 0;
};

class SafetyMonitor {
 public:
  SafetyMonitor() = default;
  explicit SafetyMonitor(const SafetyThresholds& thresholds) : thresholds_(thresholds) {}

  SafetyVerdict update(const SafetyReading& reading);

  bool obstacle() const { return obstacle_; }
  bool lowbatt() const { return lowbatt_; }

  // ⚠️ **전진만 막는다.** 전부 막으면 `FR-2.3` 의 *"정지 후 후진"* 이 실행
  // 불가가 되어 회피가 성립하지 않는다 — 초음파는 정면만 보므로 물러나는 것이
  // 유일한 탈출 경로다. 제자리 선회(step 0)와 정지도 통과시킨다.
  bool allows_move(float step_mm) const { return !(obstacle_ && step_mm > 0.0F); }

 private:
  bool fresh_sample(bool valid, uint32_t age_ms, uint32_t now_ms, uint32_t* last_sample_ms) const;

  SafetyThresholds thresholds_;
  bool obstacle_ = false;
  bool lowbatt_ = false;
  bool shutdown_reported_ = false;
  uint8_t near_count_ = 0;
  uint8_t clear_count_ = 0;
  uint8_t low_count_ = 0;
  uint32_t last_dist_sample_ms_ = UINT32_MAX;
  uint32_t last_batt_sample_ms_ = UINT32_MAX;
  uint32_t decision_age_ms_ = 0;
};

}  // namespace mechadog

#endif  // MECHADOG_SAFETY_MONITOR_H
