// ══════════════════════════════════════════════════════════════
//  WBS 4.1.3 · C++ 명령 파서 — Host PC → MechDog 제어 명령 수신 검증
//
//  정본 : docs/PROTOCOL.md 2절(명령 8종) · 3절(검증 규칙 ①~⑤)
//  참조 : host/common/protocol.py `CommandDecoder` — 규칙과 그 순서가 같아야 한다
//  검증 : tests/fixtures/protocol_samples.jsonl  (전부 ACCEPT)
//         tests/fixtures/protocol_invalid.jsonl  (`_expect` 대로 판정)
//
//  ── HAL 비의존 ──────────────────────────────────────────────
//  이 파일과 command_parser.cpp 는 Arduino.h 를 포함하지 않는다.
//  하드웨어 없이 호스트 컴파일로 단위시험이 가능해야 하기 때문이다
//  (CONTRIBUTING 7절 HAL 분리 원칙 · WBS 4.1.3 DoD).
//
//  ── 왜 JSON 라이브러리를 쓰지 않는가 ────────────────────────
//  ① third_party/ 가 비어 있다. 외부 라이브러리 도입은 WBS 5.1.2 의
//     결정 사항이며 라이선스 확인이 선행이다 (RISK-07, OI-5).
//  ② 규약이 **평면 객체**로 고정돼 있다. 중첩도 배열도 스키마에 없다.
//     범용 파서의 표현력이 필요 없다.
//  ③ 온보드 제약 — 힙 할당·예외·재귀를 쓰지 않는다. 아래 스캐너는
//     입력 길이로 경계가 잡히고 깊이는 카운터로만 건너뛴다.
//  라이브러리로 바꾸고 싶으면 이 헤더의 API 를 유지한 채 .cpp 만 교체하면 된다.
//
//  ⚠️ UDP 로는 무엇이든 들어온다. 이 파서는 **어떤 바이트열을 받아도
//     크래시하지 않아야 한다.** 수신 루프가 죽으면 관제가 멈추므로
//     폐기보다 나쁘다 (PROTOCOL.md 3절 ⑤ 주석).
// ══════════════════════════════════════════════════════════════

#ifndef MECHADOG_COMMAND_PARSER_H
#define MECHADOG_COMMAND_PARSER_H

#include <stddef.h>
#include <stdint.h>

namespace mechadog {

// ── 판정 결과 ────────────────────────────────────────────────
// DiscardWarn 은 "상대가 새 타입·새 상태를 쓰기 시작했다" 는 신호 채널이다.
// 기형 데이터(Discard)를 여기 섞으면 그 신호가 묻힌다 (PROTOCOL.md 3절).
enum class Verdict : uint8_t {
  Accept = 0,
  Discard,
  DiscardWarn,
};

// ── 제어 명령 8종 (PROTOCOL.md 2절) ─────────────────────────
enum class CmdType : uint8_t {
  Unknown = 0,
  Move,
  Pose,
  Gait,
  Stop,
  Action,
  Led,
  Sound,
  State,
};

// ── FSM 상태 13종 ────────────────────────────────────────────
// 정본은 PRD 5절 전이표다. host/common/protocol.py `FSM_STATES` 와 일치해야 한다.
// 온보드가 스스로 아는 것은 Patrol·Avoid·Failsafe 셋뿐이고, 나머지는
// 호스트가 STATE 명령으로 내려보낸다.
enum class FsmState : uint8_t {
  Unknown = 0,
  // 온보드가 센서만으로 판정 가능 (Tier 1)
  Patrol,
  Avoid,
  Failsafe,
  // 호스트 FSM 전용 (Tier 2)
  Idle,
  Scan,
  Alert,
  Track,
  Lost,
  AuthWait,
  Manual,
  // Phase 2 — 측위 확보 후
  HazardDispatch,
  HazardScan,
  ZoneInspect,
};

// ── 클램핑 대상 (PROTOCOL.md 3절 ②) ──────────────────────────
// 범위를 벗어나도 폐기하지 않는다. 명령이 조용히 사라지는 것보다 잘린 명령이 낫다.
// 어떤 필드가 잘렸는지는 로그로 남겨야 하므로 비트로 기록한다.
enum ClampFlag : uint8_t {
  kClampNone = 0,
  kClampStep = 1 << 0,
  kClampAngle = 1 << 1,
  kClampActionId = 1 << 2,
};

// ── 규약 상수 ────────────────────────────────────────────────
// 매직 넘버 금지 (CONTRIBUTING 7절 ①). 값의 정본은 host/common/protocol.py
// `CLAMP_RANGES` 이며 여기서 이름을 붙여 둔다. 실기 튜닝 대상이 되면
// NFR-3① 에 따라 config.h + NVS 로 옮긴다.
namespace limits {
constexpr float kStepMin = -100.0F;   // mm
constexpr float kStepMax = 100.0F;    // mm
constexpr float kAngleMin = -30.0F;   // deg — arc 조향. 제자리 회전 불가 (DR-11)
constexpr float kAngleMax = 30.0F;    // deg
constexpr float kActionIdMin = 0.0F;  // 내장 액션 그룹
constexpr float kActionIdMax = 15.0F;
}  // namespace limits

// LED 색 버퍼. 규약상 최장은 "orange"(6자)이므로 여유가 넉넉하다.
// 넘치면 잘라 담지 않고 폐기한다 — 잘린 문자열은 목록 대조에서 조용히 틀린다.
// 상태값은 버퍼에 담지 않는다. 문자열 비교로 곧장 enum 으로 바꾸기 때문이다.
constexpr size_t kMaxColorLen = 16;

// ── 파싱된 명령 ──────────────────────────────────────────────
// 타입에 따라 쓰이는 필드가 다르다. Verdict::Accept 일 때만 값이 유효하다.
struct Command {
  CmdType type = CmdType::Unknown;
  // seq·ts 를 64비트로 두는 이유 — UDP 로는 어떤 정수든 들어온다. 32비트로 받으면
  // 큰 값에서 조용히 접혀(wrap) 순서 게이트가 거꾸로 판단한다. 폐기보다 나쁘다.
  int64_t seq = 0;
  int64_t ts = 0;

  float step = 0.0F;   // MOVE
  float angle = 0.0F;  // MOVE

  float pitch = 0.0F;   // POSE
  float roll = 0.0F;    // POSE
  float height = 0.0F;  // POSE · GAIT — 같은 키를 두 타입이 쓴다
  float dur = 0.0F;     // POSE

  float lift_time = 0.0F;    // GAIT
  float ground_time = 0.0F;  // GAIT

  int32_t action_id = 0;  // ACTION

  char color[kMaxColorLen] = {0};  // LED
  float blink_hz = 0.0F;           // LED

  int32_t phrase_id = 0;  // SOUND

  FsmState state = FsmState::Unknown;  // STATE

  uint8_t clamped = kClampNone;  // ClampFlag 비트합
};

// ── 판정 ─────────────────────────────────────────────────────
// reason 은 정적 문자열 리터럴을 가리킨다. 소유권이 없으므로 해제하지 않는다.
struct DecodeResult {
  Verdict verdict = Verdict::Discard;
  const char* reason = "";
  Command command;
};

// ── 파서 ─────────────────────────────────────────────────────
// seq 게이트 상태를 들고 있으므로 수신 소켓 하나당 하나를 둔다.
//
// ⚠️ 송신자가 여럿이면 인스턴스도 여럿이어야 한다. 3대를 한 카운터로 묶으면
//    개체끼리 서로의 패킷을 폐기한다 (WBS 8절, protocol.py `_SeqGate` 주석).
//    온보드는 호스트 하나만 상대하므로 인스턴스 하나로 충분하다.
class CommandParser {
 public:
  CommandParser() = default;

  // raw 는 널 종단일 필요가 없다. len 이 유일한 경계다.
  DecodeResult decode(const char* raw, size_t len);

  // 마지막으로 통과시킨 seq. 아직 없으면 false 를 돌려주고 out 은 건드리지 않는다.
  bool last_seq(int64_t* out) const;

  // 링크 재수립 시 호출한다. 호스트가 재시작하면 seq 가 1 부터 다시 오므로,
  // 게이트를 비우지 않으면 그 뒤 모든 명령이 "역전" 으로 폐기된다.
  void reset();

 private:
  bool has_last_ = false;
  int64_t last_seq_ = 0;
};

// ── 로깅·시험용 이름 변환 ────────────────────────────────────
const char* to_string(Verdict v);
const char* to_string(CmdType t);
const char* to_string(FsmState s);

}  // namespace mechadog

#endif  // MECHADOG_COMMAND_PARSER_H
