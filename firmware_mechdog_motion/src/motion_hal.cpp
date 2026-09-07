#include "motion_hal.h"

#if MECHADOG_ENABLE_ACTUATORS
// 벤더 라이브러리는 저장소에 없다 — 라이선스 표기가 없어 재배포할 수 없다 (ADR-20).
// 없는 상태로 구동 빌드를 켜면 여기서 멈추고 받는 곳과 둘 곳을 알려준다.
// 안내 없이 "No such file" 만 나오면 처음 보는 사람은 원인을 알 수 없다.
#if !__has_include("HW_MechDog.h")
#error "HW_MechDog.h 가 없습니다. 공식 설치본 MechDog V1.3 에서 예제 소스를 추출해 이 스케치 폴더에 두세요. 절차: firmware_mechdog_motion/README.md"
#endif
#include "HW_MechDog.h"

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
