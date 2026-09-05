// WBS 4.1.3 · C++ 명령 파서 구현. 규격과 설계 근거는 command_parser.h 머리말 참조.
//
// 규칙 적용 순서는 host/common/protocol.py `CommandDecoder.validate` 를 그대로 따른다.
// PROTOCOL.md 3절의 ①~⑤ 를 "실행 가능한 순서" 로 편 것이며, 순서가 어긋나면
// 같은 픽스처에서 두 구현의 판정이 갈린다.
//
//   1. JSON 파싱 실패                      → Discard        (규칙 ③)
//   2. 공통 필수 필드(seq·ts·type) 누락    → Discard
//   3. seq·ts 가 정수 아님 / type 이 문자열 아님 → Discard   (규칙 ⑤)
//   4. seq 역전·중복                        → Discard        (규칙 ①)
//   5. 모르는 type                          → DiscardWarn    (규칙 ④)
//   6. 타입별 필수 필드 누락·종류 불일치    → Discard
//   7. 모르는 state                         → DiscardWarn
//   8. 범위 초과 클램핑 후                  → Accept         (규칙 ②)

#include "command_parser.h"

#include <stdlib.h>
#include <string.h>

namespace mechadog {
namespace {

// ── JSON 값 종류 ─────────────────────────────────────────────
enum class Kind : uint8_t { Invalid, Null, Bool, Number, String, Object, Array };

struct Value {
  Kind kind = Kind::Invalid;
  double num = 0.0;
  bool num_is_int = false;  // '.'·'e'·'E' 가 없고 2^53 이내 — 정수로 다룰 수 있다
  const char* str = nullptr;
  size_t str_len = 0;
  bool str_plain = true;  // 이스케이프 없음. 있으면 알려진 값 목록과 대조하지 않는다
};

// 숫자 리터럴 최대 길이. 이보다 길면 정상 규약값이 아니므로 파싱하지 않는다.
constexpr size_t kMaxNumberLen = 40;

// double 이 정수를 정확히 표현할 수 있는 한계. 넘으면 정수로 취급하지 않는다.
constexpr double kExactIntLimit = 9007199254740992.0;  // 2^53

// 중첩 건너뛰기 깊이 한도. 규약에 중첩이 없으므로 이 값에 닿는 입력은 이미 비정상이다.
constexpr int kMaxSkipDepth = 32;

bool IsWs(char c) {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

const char* SkipWs(const char* p, const char* end) {
  while (p < end && IsWs(*p)) ++p;
  return p;
}

// 문자열 리터럴을 훑는다. 여는 따옴표에서 시작하여 닫는 따옴표 다음을 돌려준다.
// 내용은 원본 슬라이스로만 넘기고 이스케이프를 풀지 않는다 — 규약의 문자열 값은
// 모두 평문이므로, 이스케이프가 섞였다면 알려진 값이 아니라는 뜻이다.
const char* ScanString(const char* p, const char* end, Value* out) {
  if (p >= end || *p != '"') return nullptr;
  ++p;
  const char* begin = p;
  bool plain = true;
  while (p < end) {
    const char c = *p;
    if (c == '\\') {
      plain = false;
      p += 2;  // 이스케이프 한 쌍을 통째로 건너뛴다
      continue;
    }
    if (c == '"') {
      if (out != nullptr) {
        out->kind = Kind::String;
        out->str = begin;
        out->str_len = static_cast<size_t>(p - begin);
        out->str_plain = plain;
      }
      return p + 1;
    }
    ++p;
  }
  return nullptr;  // 닫히지 않은 문자열
}

const char* ScanNumber(const char* p, const char* end, Value* out) {
  const char* begin = p;
  bool integral = true;
  if (p < end && (*p == '-' || *p == '+')) ++p;
  bool any_digit = false;
  while (p < end) {
    const char c = *p;
    if (c >= '0' && c <= '9') {
      any_digit = true;
      ++p;
    } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
      integral = false;
      ++p;
    } else {
      break;
    }
  }
  if (!any_digit) return nullptr;
  const size_t len = static_cast<size_t>(p - begin);
  if (len >= kMaxNumberLen) return nullptr;

  char buf[kMaxNumberLen];
  memcpy(buf, begin, len);
  buf[len] = '\0';
  char* stop = nullptr;
  const double v = strtod(buf, &stop);
  if (stop != buf + len) return nullptr;  // 뒤에 쓰레기가 붙어 있다

  if (out != nullptr) {
    out->kind = Kind::Number;
    out->num = v;
    // 2^53 을 넘으면 double 이 정수를 정확히 표현하지 못한다. seq 로 받아들이면
    // 게이트가 실제와 다른 값을 기억하므로 정수로 취급하지 않는다.
    out->num_is_int = integral && v > -kExactIntLimit && v < kExactIntLimit;
  }
  return p;
}

bool Match(const char* p, const char* end, const char* lit) {
  const size_t n = strlen(lit);
  return static_cast<size_t>(end - p) >= n && memcmp(p, lit, n) == 0;
}

// 중첩 객체·배열을 재귀 없이 건너뛴다. 문자열 안의 괄호는 세지 않는다.
const char* SkipStructured(const char* p, const char* end) {
  int depth = 0;
  while (p < end) {
    const char c = *p;
    if (c == '"') {
      p = ScanString(p, end, nullptr);
      if (p == nullptr) return nullptr;
      continue;
    }
    if (c == '{' || c == '[') {
      if (++depth > kMaxSkipDepth) return nullptr;
      ++p;
      continue;
    }
    if (c == '}' || c == ']') {
      --depth;
      ++p;
      if (depth == 0) return p;
      if (depth < 0) return nullptr;
      continue;
    }
    ++p;
  }
  return nullptr;
}

const char* ScanValue(const char* p, const char* end, Value* out) {
  p = SkipWs(p, end);
  if (p >= end) return nullptr;
  const char c = *p;
  if (c == '"') return ScanString(p, end, out);
  if (c == '{' || c == '[') {
    out->kind = (c == '{') ? Kind::Object : Kind::Array;
    return SkipStructured(p, end);
  }
  if (Match(p, end, "true")) {
    out->kind = Kind::Bool;
    return p + 4;
  }
  if (Match(p, end, "false")) {
    out->kind = Kind::Bool;
    return p + 5;
  }
  if (Match(p, end, "null")) {
    out->kind = Kind::Null;
    return p + 4;
  }
  return ScanNumber(p, end, out);
}

bool KeyIs(const Value& key, const char* name) {
  return key.str_plain && key.str_len == strlen(name) && memcmp(key.str, name, key.str_len) == 0;
}

bool StrEq(const Value& v, const char* lit) {
  return v.kind == Kind::String && v.str_plain && v.str_len == strlen(lit) &&
         memcmp(v.str, lit, v.str_len) == 0;
}

// ── 한 번 훑어 담는 원시 필드 ────────────────────────────────
// "있었는가" 와 "종류가 맞았는가" 를 나눠 기록한다. 규칙 ⑤ 가 그 구분을 요구한다.
struct Field {
  bool present = false;
  Value value;
};

struct RawMsg {
  Field seq, ts, type;
  Field step, angle;
  Field pitch, roll, height, dur;
  Field lift_time, ground_time;
  Field id;
  Field color, blink_hz;
  Field phrase_id;
  Field state;
};

// 최상위 객체를 훑는다. 모르는 키는 조용히 무시한다 — 픽스처의 `_case`·`_expect`
// 도 그렇게 걸러지고, 옵션 필드 추가가 하위 호환이 되는 근거이기도 하다.
bool ScanObject(const char* p, const char* end, RawMsg* out) {
  p = SkipWs(p, end);
  if (p >= end || *p != '{') return false;
  ++p;
  p = SkipWs(p, end);
  if (p < end && *p == '}') return true;  // 빈 객체

  for (;;) {
    p = SkipWs(p, end);
    Value key;
    p = ScanString(p, end, &key);
    if (p == nullptr) return false;

    p = SkipWs(p, end);
    if (p >= end || *p != ':') return false;
    ++p;

    Value val;
    p = ScanValue(p, end, &val);
    if (p == nullptr) return false;

    Field* slot = nullptr;
    if (KeyIs(key, "seq")) {
      slot = &out->seq;
    } else if (KeyIs(key, "ts")) {
      slot = &out->ts;
    } else if (KeyIs(key, "type")) {
      slot = &out->type;
    } else if (KeyIs(key, "step")) {
      slot = &out->step;
    } else if (KeyIs(key, "angle")) {
      slot = &out->angle;
    } else if (KeyIs(key, "pitch")) {
      slot = &out->pitch;
    } else if (KeyIs(key, "roll")) {
      slot = &out->roll;
    } else if (KeyIs(key, "height")) {
      slot = &out->height;
    } else if (KeyIs(key, "dur")) {
      slot = &out->dur;
    } else if (KeyIs(key, "lift_time")) {
      slot = &out->lift_time;
    } else if (KeyIs(key, "ground_time")) {
      slot = &out->ground_time;
    } else if (KeyIs(key, "id")) {
      slot = &out->id;
    } else if (KeyIs(key, "color")) {
      slot = &out->color;
    } else if (KeyIs(key, "blink_hz")) {
      slot = &out->blink_hz;
    } else if (KeyIs(key, "phrase_id")) {
      slot = &out->phrase_id;
    } else if (KeyIs(key, "state")) {
      slot = &out->state;
    }
    if (slot != nullptr) {
      slot->present = true;
      slot->value = val;
    }

    p = SkipWs(p, end);
    if (p >= end) return false;
    if (*p == ',') {
      ++p;
      continue;
    }
    if (*p == '}') return true;
    return false;
  }
}

CmdType ParseType(const Value& v) {
  if (StrEq(v, "MOVE")) return CmdType::Move;
  if (StrEq(v, "POSE")) return CmdType::Pose;
  if (StrEq(v, "GAIT")) return CmdType::Gait;
  if (StrEq(v, "STOP")) return CmdType::Stop;
  if (StrEq(v, "ACTION")) return CmdType::Action;
  if (StrEq(v, "LED")) return CmdType::Led;
  if (StrEq(v, "SOUND")) return CmdType::Sound;
  if (StrEq(v, "STATE")) return CmdType::State;
  return CmdType::Unknown;
}

FsmState ParseState(const Value& v) {
  if (StrEq(v, "PATROL")) return FsmState::Patrol;
  if (StrEq(v, "AVOID")) return FsmState::Avoid;
  if (StrEq(v, "FAILSAFE")) return FsmState::Failsafe;
  if (StrEq(v, "IDLE")) return FsmState::Idle;
  if (StrEq(v, "SCAN")) return FsmState::Scan;
  if (StrEq(v, "ALERT")) return FsmState::Alert;
  if (StrEq(v, "TRACK")) return FsmState::Track;
  if (StrEq(v, "LOST")) return FsmState::Lost;
  if (StrEq(v, "AUTH_WAIT")) return FsmState::AuthWait;
  if (StrEq(v, "MANUAL")) return FsmState::Manual;
  if (StrEq(v, "HAZARD_DISPATCH")) return FsmState::HazardDispatch;
  if (StrEq(v, "HAZARD_SCAN")) return FsmState::HazardScan;
  if (StrEq(v, "ZONE_INSPECT")) return FsmState::ZoneInspect;
  return FsmState::Unknown;
}

float ClampTo(double v, float low, float high, uint8_t flag, uint8_t* mask) {
  double out = v;
  if (out < low) out = low;
  if (out > high) out = high;
  if (out != v) *mask |= flag;
  return static_cast<float>(out);
}

DecodeResult Reject(Verdict verdict, const char* reason) {
  DecodeResult r;
  r.verdict = verdict;
  r.reason = reason;
  return r;
}

// 타입별 필수 필드가 모두 있고 종류가 맞는지 본다. 문자열이어야 할 자리에 숫자가
// 오는 것도 폐기 사유다 — 알려진 값 목록과 대조하기 전에 걸러야 안전하다 (규칙 ⑤).
const char* CheckRequired(CmdType type, const RawMsg& m) {
  // 필드 이름을 함께 들고 사유 문자열에 끼워 넣고 싶지만, 그러려면 런타임
  // 문자열 조립이 필요하다. 온보드에서 버퍼를 잡거나 정적 버퍼를 공유하는 쪽 모두
  // 값에 비해 대가가 크다. 타입(`to_string(CmdType)`)과 사유만으로도 어느 필드가
  // 문제인지는 규약 표에서 바로 좁혀진다.
  struct Req {
    const Field* field;
    bool wants_string;
  };
  Req reqs[4];
  int n = 0;
  switch (type) {
    case CmdType::Move:
      reqs[n++] = {&m.step, false};
      reqs[n++] = {&m.angle, false};
      break;
    case CmdType::Pose:
      reqs[n++] = {&m.pitch, false};
      reqs[n++] = {&m.roll, false};
      reqs[n++] = {&m.height, false};
      reqs[n++] = {&m.dur, false};
      break;
    case CmdType::Gait:
      reqs[n++] = {&m.lift_time, false};
      reqs[n++] = {&m.ground_time, false};
      reqs[n++] = {&m.height, false};
      break;
    case CmdType::Action:
      reqs[n++] = {&m.id, false};
      break;
    case CmdType::Led:
      reqs[n++] = {&m.color, true};
      reqs[n++] = {&m.blink_hz, false};
      break;
    case CmdType::Sound:
      reqs[n++] = {&m.phrase_id, false};
      break;
    case CmdType::State:
      reqs[n++] = {&m.state, true};
      break;
    case CmdType::Stop:
    case CmdType::Unknown:
    default:
      break;
  }
  for (int i = 0; i < n; ++i) {
    if (!reqs[i].field->present) return "타입별 필수 필드 누락";
    const Kind k = reqs[i].field->value.kind;
    if (reqs[i].wants_string) {
      if (k != Kind::String) return "문자열이어야 할 필드가 문자열이 아님";
    } else if (k != Kind::Number) {
      return "수치여야 할 필드가 수치가 아님";
    }
  }
  return nullptr;
}

void CopyString(const Value& v, char* dst, size_t cap, bool* overflow) {
  if (v.str_len >= cap) {
    *overflow = true;
    dst[0] = '\0';
    return;
  }
  memcpy(dst, v.str, v.str_len);
  dst[v.str_len] = '\0';
}

}  // namespace

DecodeResult CommandParser::decode(const char* raw, size_t len) {
  if (raw == nullptr || len == 0) return Reject(Verdict::Discard, "빈 입력");

  RawMsg m;
  if (!ScanObject(raw, raw + len, &m)) return Reject(Verdict::Discard, "파싱 실패");

  // 공통 필수 필드
  if (!m.seq.present) return Reject(Verdict::Discard, "공통 필수 필드 누락: seq");
  if (!m.ts.present) return Reject(Verdict::Discard, "공통 필수 필드 누락: ts");
  if (!m.type.present) return Reject(Verdict::Discard, "공통 필수 필드 누락: type");

  // 규칙 ⑤ — 종류가 규약과 다르면 폐기. 목록 대조보다 먼저 온다.
  if (m.seq.value.kind != Kind::Number || !m.seq.value.num_is_int) {
    return Reject(Verdict::Discard, "seq 가 정수가 아님");
  }
  if (m.ts.value.kind != Kind::Number || !m.ts.value.num_is_int) {
    return Reject(Verdict::Discard, "ts 가 정수가 아님");
  }
  if (m.type.value.kind != Kind::String) {
    return Reject(Verdict::Discard, "type 이 문자열이 아님");
  }

  const int64_t seq = static_cast<int64_t>(m.seq.value.num);

  // 규칙 ① — seq 역전·중복
  //
  // ⚠️ 검사와 갱신이 한 동작이다. 참조 구현 `_SeqGate.admit` 이 통과시키는 즉시
  //    `_last` 를 올리므로, **뒤에서 폐기되는 명령(미지 타입·필드 누락)도 seq 를
  //    전진시킨다.** 갱신을 뒤로 미루면 같은 픽스처에서 두 구현의 판정이 갈린다.
  //    여기가 골든 픽스처가 실제로 잡아내는 지점이다.
  if (has_last_ && seq <= last_seq_) return Reject(Verdict::Discard, "seq 역전·중복");
  has_last_ = true;
  last_seq_ = seq;

  // 규칙 ④ — 모르는 타입은 폐기 + WARN
  const CmdType type = ParseType(m.type.value);
  if (type == CmdType::Unknown) return Reject(Verdict::DiscardWarn, "알 수 없는 타입");

  // 타입별 필수 필드
  const char* missing = CheckRequired(type, m);
  if (missing != nullptr) return Reject(Verdict::Discard, missing);

  // 모르는 상태값 — 폐기 + WARN. 타입과 같은 이유로 상태 추가를 하위 호환으로 만든다.
  FsmState state = FsmState::Unknown;
  if (type == CmdType::State) {
    state = ParseState(m.state.value);
    if (state == FsmState::Unknown) return Reject(Verdict::DiscardWarn, "알 수 없는 상태");
  }

  DecodeResult r;
  r.verdict = Verdict::Accept;
  r.reason = "";
  Command& c = r.command;
  c.type = type;
  c.seq = seq;
  c.ts = static_cast<int64_t>(m.ts.value.num);
  c.state = state;

  // 규칙 ② — 범위 초과는 폐기가 아니라 클램핑. 참조 구현과 같이 타입과 무관하게
  // 해당 키가 있으면 자른다.
  if (m.step.present && m.step.value.kind == Kind::Number) {
    c.step = ClampTo(m.step.value.num, limits::kStepMin, limits::kStepMax, kClampStep, &c.clamped);
  }
  if (m.angle.present && m.angle.value.kind == Kind::Number) {
    c.angle =
        ClampTo(m.angle.value.num, limits::kAngleMin, limits::kAngleMax, kClampAngle, &c.clamped);
  }
  if (m.id.present && m.id.value.kind == Kind::Number) {
    c.action_id = static_cast<int32_t>(ClampTo(m.id.value.num, limits::kActionIdMin,
                                               limits::kActionIdMax, kClampActionId, &c.clamped));
  }

  if (m.pitch.present) c.pitch = static_cast<float>(m.pitch.value.num);
  if (m.roll.present) c.roll = static_cast<float>(m.roll.value.num);
  if (m.height.present) c.height = static_cast<float>(m.height.value.num);
  if (m.dur.present) c.dur = static_cast<float>(m.dur.value.num);
  if (m.lift_time.present) c.lift_time = static_cast<float>(m.lift_time.value.num);
  if (m.ground_time.present) c.ground_time = static_cast<float>(m.ground_time.value.num);
  if (m.blink_hz.present) c.blink_hz = static_cast<float>(m.blink_hz.value.num);
  if (m.phrase_id.present) c.phrase_id = static_cast<int32_t>(m.phrase_id.value.num);

  if (type == CmdType::Led) {
    // ⚠️ 참조 구현과 의도적으로 다른 유일한 지점이다. Python 은 `color` 의 길이를
    //    제한하지 않으므로 아무리 긴 문자열도 받아들인다. 온보드는 고정 버퍼라
    //    담을 수 없고, 잘라 담으면 알려진 색과 조용히 어긋난다. 규약상 최장 색이
    //    "orange"(6자)이므로 버퍼를 넘는 값은 유효한 색일 수 없다 → 폐기한다.
    //    REVIEW_LOG 에 등재했다. 규약에 길이 제한을 넣으면 이 분기는 사라진다.
    bool overflow = false;
    CopyString(m.color.value, c.color, kMaxColorLen, &overflow);
    if (overflow) return Reject(Verdict::Discard, "color 가 버퍼보다 김");
  }

  return r;
}

bool CommandParser::last_seq(int64_t* out) const {
  if (!has_last_ || out == nullptr) return false;
  *out = last_seq_;
  return true;
}

void CommandParser::reset() {
  has_last_ = false;
  last_seq_ = 0;
}

const char* to_string(Verdict v) {
  switch (v) {
    case Verdict::Accept:
      return "ACCEPT";
    case Verdict::Discard:
      return "DISCARD";
    case Verdict::DiscardWarn:
      return "DISCARD_WARN";
    default:
      return "?";
  }
}

const char* to_string(CmdType t) {
  switch (t) {
    case CmdType::Move:
      return "MOVE";
    case CmdType::Pose:
      return "POSE";
    case CmdType::Gait:
      return "GAIT";
    case CmdType::Stop:
      return "STOP";
    case CmdType::Action:
      return "ACTION";
    case CmdType::Led:
      return "LED";
    case CmdType::Sound:
      return "SOUND";
    case CmdType::State:
      return "STATE";
    case CmdType::Unknown:
    default:
      return "UNKNOWN";
  }
}

const char* to_string(FsmState s) {
  switch (s) {
    case FsmState::Patrol:
      return "PATROL";
    case FsmState::Avoid:
      return "AVOID";
    case FsmState::Failsafe:
      return "FAILSAFE";
    case FsmState::Idle:
      return "IDLE";
    case FsmState::Scan:
      return "SCAN";
    case FsmState::Alert:
      return "ALERT";
    case FsmState::Track:
      return "TRACK";
    case FsmState::Lost:
      return "LOST";
    case FsmState::AuthWait:
      return "AUTH_WAIT";
    case FsmState::Manual:
      return "MANUAL";
    case FsmState::HazardDispatch:
      return "HAZARD_DISPATCH";
    case FsmState::HazardScan:
      return "HAZARD_SCAN";
    case FsmState::ZoneInspect:
      return "ZONE_INSPECT";
    case FsmState::Unknown:
    default:
      return "UNKNOWN";
  }
}

}  // namespace mechadog
