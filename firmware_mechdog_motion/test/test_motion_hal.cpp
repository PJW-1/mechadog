#include <cstdlib>
#include <iostream>

#include "motion_hal.h"

static void check(bool value) {
  if (!value) std::abort();
}

int main() {
  mechadog::MotionHal hal;
  // 구동 미지원 빌드(기본)는 런타임 요청과 무관하게 절대 켜지지 않는다 —
  // 요청은 링크되지 않은 하드웨어를 만들어 내지 못한다.
  check(!mechadog::actuatorsRuntimeEnabled());
  check(!hal.actuators_enabled());
  mechadog::actuatorsSetRuntime(true);
  check(!mechadog::actuatorsRuntimeEnabled());
  check(!hal.actuators_enabled());
  mechadog::actuatorsSetRuntime(false);
  check(!mechadog::actuatorsRuntimeEnabled());
  check(hal.begin());
  hal.move(60, 0);
  hal.stop();
  std::cout << "Motion HAL runtime gate: compiled-off build ignores requests\n";
}
