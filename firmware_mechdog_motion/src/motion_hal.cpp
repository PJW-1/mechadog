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

void MotionHal::pose(float pitch_deg, float roll_deg, float height_mm, int duration_ms) {
#if MECHADOG_ENABLE_ACTUATORS
  // 벤더 자세는 {위치{x,y,z}, 자세{roll,pitch,yaw}} 다. 규약의 `height` 는 몸통을
  // 올리고 내리는 값이므로 z 로 간다.
  //
  // ⚠️ **yaw 는 0 으로 고정한다.** 다리 2자유도가 모두 앞뒤 평면에 있어 몸통 yaw 는
  // 기하학적으로 불가능하다(DR-11). 규약에도 `POSE` 에 yaw 필드가 없다 — 여기서
  // 임의로 채우면 되지도 않는 동작을 시도하는 셈이다.
  mech_pose_t target = {{0, 0, height_mm}, {roll_deg, pitch_deg, 0}};
  g_mechdog.transform(target, duration_ms);
#else
  (void)pitch_deg;
  (void)roll_deg;
  (void)height_mm;
  (void)duration_ms;
#endif
}

bool MotionHal::action(int id) {
  for (int i = 0; i < kActionNameCount; ++i) {
    if (kActionNames[i].id != id) continue;
#if MECHADOG_ENABLE_ACTUATORS
    // ⚠️ 여기서 최대 1초간 블로킹된다 — 벤더가 구간마다 delay() 한다. 부르는 쪽이
    // 정지 상태에서만 부르도록 막고 있다(`.ino` 의 보행 가드).
    g_mechdog.action_run(kActionNames[i].name);
#endif
    return true;
  }
  return false;
}

// ⚠️ **`config/config.yaml` 의 `actions.id_map` 과 같아야 한다.**
// `tests/test_firmware_actions.py` 가 둘을 대조해 어긋나면 CI 가 실패한다 — 전선으로는
// 숫자가 오는데 벤더는 이름으로 찾으므로, 조용히 어긋나면 **다른 동작이 나간다.**
//
// ⚠️ 단일 구간(1,000ms) 액션만 싣는다. 여러 구간짜리는 블로킹이 길어져 `loop()` 가
// 그만큼 멈춘다 — 300ms 명령 타임아웃 검사도 함께 멈춘다.
//
// ⚠️ `sit_dowm` 은 벤더 오타 그대로다. 고쳐 적으면 벤더가 못 찾고 **아무 일도 하지 않는다.**
const ActionName kActionNames[] = {
    {0, "stand_four_legs"},
    {1, "sit_dowm"},
    {2, "go_prone"},
};
const int kActionNameCount = sizeof(kActionNames) / sizeof(kActionNames[0]);

bool MotionHal::actuators_enabled() const {
  return MECHADOG_ENABLE_ACTUATORS != 0;
}

}  // namespace mechadog
