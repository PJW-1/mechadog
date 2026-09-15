#include "motion_hal.h"

#if MECHADOG_ENABLE_ACTUATORS
// 벤더 라이브러리는 저장소에 없다 — 라이선스 표기가 없어 재배포할 수 없다 (ADR-20).
// 없는 상태로 구동 빌드를 켜면 여기서 멈추고 받는 곳과 둘 곳을 알려준다.
// 안내 없이 "No such file" 만 나오면 처음 보는 사람은 원인을 알 수 없다.
// 벤더 파일은 스케치 루트에 둔다. `src/` 의 이 파일에서는 루트가 포함 경로에 없으므로
// 상대 경로로 부른다 — `"HW_MechDog.h"` 로 두면 파일이 있어도 못 찾는다.
#if !__has_include("../HW_MechDog.h")
#error \
    "HW_MechDog.h 없음 — README.md 의 '구동·센서 통합 빌드 준비' 절차대로 벤더 파일을 받아 두세요"
#endif
#include "../HW_MechDog.h"

namespace {
MechDog g_mechdog;
}
#endif

namespace mechadog {

bool MotionHal::begin() {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.MechDog_init();
  g_mechdog.move(0, 0);
#endif
  return true;
}

void MotionHal::move(float step_mm, float angle_deg) {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.move(step_mm, angle_deg);
#else
  (void)step_mm;
  (void)angle_deg;
#endif
}

void MotionHal::stop() {
#if MECHADOG_ENABLE_ACTUATORS
  g_mechdog.move(0, 0);
#endif
}

bool MotionHal::actuators_enabled() const {
  return MECHADOG_ENABLE_ACTUATORS != 0;
}

}  // namespace mechadog
