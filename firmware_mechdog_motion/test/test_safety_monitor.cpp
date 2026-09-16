#include <cstdio>
#include <cstdlib>

#include "safety_monitor.h"

namespace {
int checks = 0;

void check(bool condition) {
  ++checks;
  if (!condition) {
    std::fprintf(stderr, "Safety monitor check %d failed\n", checks);
    std::abort();
  }
}

// 센서 주기 40ms. 한 표본을 넣고 그 결과를 돌려준다.
mechadog::SafetyVerdict distance_sample(mechadog::SafetyMonitor& monitor, uint32_t& now_ms,
                                        float dist_cm, float batt_v = 8.0F) {
  now_ms += 40;
  mechadog::SafetyReading reading;
  reading.now_ms = now_ms;
  reading.dist_valid = true;
  reading.dist_cm = dist_cm;
  reading.dist_age_ms = 5;
  reading.batt_valid = true;
  reading.batt_v = batt_v;
  reading.batt_age_ms = 5;
  return monitor.update(reading);
}
}  // namespace

int main() {
  uint32_t now_ms = 1000;
  mechadog::SafetyMonitor monitor;

  // ── 근거리 정지: 단발로는 멈추지 않는다 (DoD ④) ────────────────────
  check(!distance_sample(monitor, now_ms, 20.0F).obstacle);
  check(distance_sample(monitor, now_ms, 80.0F).obstacle == false);

  // 연속 2표본이면 걸리고, 시작 신호는 그 순간 한 번만 나온다.
  check(!distance_sample(monitor, now_ms, 18.0F).obstacle);
  const mechadog::SafetyVerdict caught = distance_sample(monitor, now_ms, 17.0F);
  check(caught.obstacle && caught.obstacle_started);
  check(caught.decision_age_ms == 5);
  const mechadog::SafetyVerdict held = distance_sample(monitor, now_ms, 16.0F);
  check(held.obstacle && !held.obstacle_started);

  // ⚠️ 전진만 막는다 — 후진·선회·정지는 통과해야 회피가 성립한다 (DoD ⑤).
  check(!monitor.allows_move(60.0F));
  check(monitor.allows_move(-60.0F));
  check(monitor.allows_move(0.0F));

  // ── 히스테리시스: 임계와 해제 사이에서는 상태가 유지된다 ────────────
  check(distance_sample(monitor, now_ms, 27.0F).obstacle);
  check(distance_sample(monitor, now_ms, 27.0F).obstacle);

  // ⚠️ **단발·쌍발 흐트러짐으로는 풀리지 않는다 (2026-09-17 실기).** 21cm 표적
  // 앞에서 초음파가 13% 꼴로 34cm 를 섞어 읽었고, 해제가 2표본이던 동안 차단이
  // 12초에 29번 풀렸다 걸렸다 했다.
  check(distance_sample(monitor, now_ms, 34.0F).obstacle);
  check(distance_sample(monitor, now_ms, 16.0F).obstacle);
  check(distance_sample(monitor, now_ms, 34.0F).obstacle);
  check(distance_sample(monitor, now_ms, 34.0F).obstacle);
  check(distance_sample(monitor, now_ms, 16.0F).obstacle);

  // 30cm 이상이 연속 5표본(200ms) 이어져야 풀린다.
  for (int i = 0; i < 4; ++i) check(distance_sample(monitor, now_ms, 31.0F).obstacle);
  check(!distance_sample(monitor, now_ms, 32.0F).obstacle);
  check(monitor.allows_move(60.0F));

  // ── 같은 표본을 다시 넣어도 판정이 앞으로 가지 않는다 ───────────────
  // loop() 는 1ms 마다 도는데 센서는 40ms 마다 갱신된다. 나이로 측정 시각을
  // 되돌려 같은 표본을 걸러내지 않으면 40ms 한 번이 2표본으로 세어진다.
  {
    mechadog::SafetyMonitor repeated;
    mechadog::SafetyReading reading;
    reading.now_ms = 5000;
    reading.dist_valid = true;
    reading.dist_cm = 10.0F;
    reading.dist_age_ms = 0;
    reading.batt_valid = true;
    reading.batt_v = 8.0F;
    reading.batt_age_ms = 0;
    check(!repeated.update(reading).obstacle);
    for (int i = 1; i <= 20; ++i) {
      reading.now_ms = 5000 + static_cast<uint32_t>(i);
      reading.dist_age_ms = static_cast<uint32_t>(i);  // 같은 표본이 나이만 먹는다
      check(!repeated.update(reading).obstacle);
    }
    // 새 표본이 와야 두 번째로 센다.
    reading.now_ms = 5040;
    reading.dist_age_ms = 0;
    check(repeated.update(reading).obstacle);
  }

  // ── 값이 없거나 오래되면 새 판정을 만들지 않고 상태를 유지한다 ──────
  {
    mechadog::SafetyMonitor stale;
    uint32_t t = 1000;
    distance_sample(stale, t, 10.0F);
    check(distance_sample(stale, t, 10.0F).obstacle);

    mechadog::SafetyReading dead;
    dead.now_ms = t + 40;
    dead.dist_valid = false;
    dead.batt_valid = true;
    dead.batt_v = 8.0F;
    dead.batt_age_ms = 5;
    check(stale.update(dead).obstacle);  // 센서가 죽었다고 전진이 열리면 안 된다

    mechadog::SafetyReading old_sample;
    old_sample.now_ms = t + 80;
    old_sample.dist_valid = true;
    old_sample.dist_cm = 200.0F;  // 멀다고 하지만 오래된 값이다
    old_sample.dist_age_ms = 5000;
    old_sample.batt_valid = true;
    old_sample.batt_v = 8.0F;
    old_sample.batt_age_ms = 5;
    check(stale.update(old_sample).obstacle);
    check(!stale.allows_move(60.0F));
    check(stale.allows_move(-60.0F));
  }

  // ── 저전압: 경고는 한 표본, 셧다운은 연속 3표본 (3.2.2) ──────────────
  {
    mechadog::SafetyMonitor battery;
    uint32_t t = 1000;
    check(!distance_sample(battery, t, 100.0F, 7.4F).lowbatt);
    check(distance_sample(battery, t, 100.0F, 7.0F).lowbatt);  // 경계 포함
    check(distance_sample(battery, t, 100.0F, 6.9F).lowbatt);

    // 6.6V 이하 두 번까지는 세우지 않는다 — 보행 중 순간 강하로 멈추지 않게.
    check(!distance_sample(battery, t, 100.0F, 6.5F).shutdown);
    check(!distance_sample(battery, t, 100.0F, 6.4F).shutdown);
    const mechadog::SafetyVerdict down = distance_sample(battery, t, 100.0F, 6.4F);
    check(down.shutdown && down.lowbatt);
    // 셧다운 요구는 한 번만 — 래치는 이미 걸렸고 매 표본 다시 걸 이유가 없다.
    check(!distance_sample(battery, t, 100.0F, 6.3F).shutdown);

    // 셧다운선 바로 위에서 떠는 것으로는 재무장하지 않는다.
    check(!distance_sample(battery, t, 100.0F, 6.8F).shutdown);
    check(!distance_sample(battery, t, 100.0F, 6.4F).shutdown);
    check(!distance_sample(battery, t, 100.0F, 6.4F).shutdown);
    check(!distance_sample(battery, t, 100.0F, 6.4F).shutdown);

    // 경고선 위로 회복되면 다시 무장하고, 다음 교차에서 또 걸린다.
    check(!distance_sample(battery, t, 100.0F, 7.6F).lowbatt);
    distance_sample(battery, t, 100.0F, 6.4F);
    distance_sample(battery, t, 100.0F, 6.4F);
    check(distance_sample(battery, t, 100.0F, 6.4F).shutdown);
  }

  // ── 임계는 설정에서 온다 — 시험용 빌드가 값을 올려 교차를 만든다 ────
  {
    mechadog::SafetyThresholds bench;
    bench.battery_warn_v = 8.2F;
    bench.battery_shutdown_v = 8.0F;
    mechadog::SafetyMonitor monitor_bench(bench);
    uint32_t t = 1000;
    check(distance_sample(monitor_bench, t, 100.0F, 8.1F).lowbatt);
    distance_sample(monitor_bench, t, 100.0F, 7.9F);
    distance_sample(monitor_bench, t, 100.0F, 7.9F);
    check(distance_sample(monitor_bench, t, 100.0F, 7.9F).shutdown);
  }

  // ── 안전 조건 조합 전수 (WBS 3.2.5) ──────────────────────────────────
  //
  // **E-Stop·타임아웃·링크 두절·저전압 > 근거리 정지 > 호스트 명령.** 앞의 넷은
  // 래치로 들어오므로 여기서는 «래치 여부 × 서비스 모드 × 반사 정지 × 명령 방향»
  // 을 전수로 돌린다.
  {
    for (int latched = 0; latched <= 1; ++latched) {
      for (int service = 0; service <= 1; ++service) {
        for (int blocked = 0; blocked <= 1; ++blocked) {
          mechadog::MotionSafetyState state;
          state.reset_safe();
          if (service) state.enter_service();
          if (latched) state.latch();

          mechadog::SafetyMonitor safety;
          uint32_t at = 1000;
          if (blocked) {
            distance_sample(safety, at, 10.0F);
            distance_sample(safety, at, 10.0F);
            check(safety.obstacle());
          }

          const bool free_to_walk = !latched && !service;
          // 전진: 래치·서비스가 먼저, 그다음 반사 정지.
          check(mechadog::move_allowed(state, safety, 60.0F) == (free_to_walk && !blocked));
          // 후진: 반사 정지는 막지 않는다. 래치·서비스는 그대로 막는다.
          check(mechadog::move_allowed(state, safety, -60.0F) == free_to_walk);
          // 정지(0)도 같은 규칙 — 반사 정지가 정지를 막을 이유가 없다.
          check(mechadog::move_allowed(state, safety, 0.0F) == free_to_walk);
          // 반사 정지는 래치 원인이 아니다 — 해제를 막지 않는다.
          check(mechadog::reset_safe_allowed(safety));
        }
      }
    }
  }

  // ── 저전압 원인이 남아 있으면 RESET_SAFE 를 거부한다 (3.2.5) ──────────
  {
    mechadog::SafetyMonitor safety;
    uint32_t at = 1000;
    check(mechadog::reset_safe_allowed(safety));  // 아무 일도 없으면 풀 수 있다

    distance_sample(safety, at, 100.0F, 6.4F);
    distance_sample(safety, at, 100.0F, 6.4F);
    check(distance_sample(safety, at, 100.0F, 6.4F).shutdown);
    check(!mechadog::reset_safe_allowed(safety));  // 원인이 남아 있다

    // 셧다운선 위로 조금 올라온 것으로는 부족하다 — 다음 부하에 다시 걸린다.
    distance_sample(safety, at, 100.0F, 6.8F);
    check(!mechadog::reset_safe_allowed(safety));

    // 경고선 위로 회복돼야 풀 수 있다.
    distance_sample(safety, at, 100.0F, 7.4F);
    check(mechadog::reset_safe_allowed(safety));
  }

  std::printf("safety monitor: %d checks passed\n", checks);
  return 0;
}
