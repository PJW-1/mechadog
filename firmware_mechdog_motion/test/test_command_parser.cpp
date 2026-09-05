// WBS 4.1.3 · C++ 명령 파서 호스트 단위시험
//
// 하드웨어 없이 돌린다 (CONTRIBUTING 7절 HAL 분리 원칙). 외부 테스트
// 프레임워크를 쓰지 않는 이유는 파서와 같다 — third_party/ 가 비어 있고
// 의존성 도입은 WBS 5.1.2 의 결정 사항이다.
//
// 빌드·실행 (아래 인자를 한 줄로 이어서 준다):
//   g++ -std=c++17 -Wall -Wextra -O1 -I firmware_mechdog_motion/src
//       firmware_mechdog_motion/src/command_parser.cpp
//       firmware_mechdog_motion/test/test_command_parser.cpp
//       -o build/test_command_parser
//   ./build/test_command_parser tests/fixtures
//
// 정답지는 골든 픽스처다. Python 쪽(tests/test_protocol_fixtures.py)과 같은
// 파일을 읽으므로, 스키마를 한쪽만 바꾸면 둘 중 하나가 반드시 깨진다.

#include <stdio.h>
#include <string.h>

#include "command_parser.h"

namespace {

int g_checks = 0;
int g_failures = 0;

void Check(bool ok, const char* what, const char* detail) {
  ++g_checks;
  if (ok) return;
  ++g_failures;
  printf("  [FAIL] %s\n         %s\n", what, detail != nullptr ? detail : "");
}

// 픽스처 한 줄에서 `_expect` 값을 읽는다. 없으면 전부 ACCEPT 여야 한다는 뜻이다.
mechadog::Verdict ExpectedOf(const char* line) {
  const char* p = strstr(line, "\"_expect\"");
  if (p == nullptr) return mechadog::Verdict::Accept;
  if (strstr(p, "discard_warn") != nullptr) return mechadog::Verdict::DiscardWarn;
  if (strstr(p, "discard") != nullptr) return mechadog::Verdict::Discard;
  if (strstr(p, "clamp") != nullptr) return mechadog::Verdict::Accept;
  return mechadog::Verdict::Discard;
}

bool ExpectsClamp(const char* line) {
  const char* p = strstr(line, "\"_expect\"");
  return p != nullptr && strstr(p, "clamp") != nullptr;
}

// 한 파일을 순서대로 먹인다. 파서 인스턴스는 하나다 — seq 게이트가 줄 사이에
// 걸쳐 동작해야 하고, 실제 수신 루프도 그렇게 돈다.
int RunFixture(const char* dir, const char* name, bool expect_all_accept) {
  char path[512];
  snprintf(path, sizeof(path), "%s/%s", dir, name);
  FILE* f = fopen(path, "rb");
  if (f == nullptr) {
    printf("  [FAIL] 픽스처를 열 수 없음: %s\n", path);
    ++g_failures;
    return 0;
  }

  mechadog::CommandParser parser;
  char line[2048];
  int lineno = 0;
  int seen = 0;
  while (fgets(line, sizeof(line), f) != nullptr) {
    ++lineno;
    size_t len = strlen(line);
    while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) --len;
    if (len == 0) continue;
    ++seen;

    const mechadog::Verdict expected =
        expect_all_accept ? mechadog::Verdict::Accept : ExpectedOf(line);
    const mechadog::DecodeResult r = parser.decode(line, len);

    char detail[640];
    snprintf(detail, sizeof(detail), "%s:%d  기대=%s 실제=%s (%s)", name, lineno,
             mechadog::to_string(expected), mechadog::to_string(r.verdict), r.reason);
    Check(r.verdict == expected, "픽스처 판정", detail);

    if (!expect_all_accept && ExpectsClamp(line)) {
      snprintf(detail, sizeof(detail), "%s:%d  클램핑 플래그가 비어 있음", name, lineno);
      Check(r.command.clamped != mechadog::kClampNone, "클램핑 기록", detail);
    }
  }
  fclose(f);
  return seen;
}

// ── 값이 실제로 맞게 담기는지 ────────────────────────────────
void TestFieldValues() {
  mechadog::CommandParser p;
  const char* move = "{\"seq\":1,\"ts\":1756800000000,\"type\":\"MOVE\",\"step\":60,\"angle\":0}";
  mechadog::DecodeResult r = p.decode(move, strlen(move));
  Check(r.verdict == mechadog::Verdict::Accept, "MOVE 수락", r.reason);
  Check(r.command.type == mechadog::CmdType::Move, "MOVE 타입", nullptr);
  Check(r.command.seq == 1, "seq 값", nullptr);
  Check(r.command.ts == 1756800000000LL, "ts 값 (32비트로 접히면 안 된다)", nullptr);
  Check(r.command.step == 60.0F, "step 값", nullptr);
  Check(r.command.clamped == mechadog::kClampNone, "정상값은 클램핑 없음", nullptr);

  const char* led = "{\"seq\":2,\"ts\":1,\"type\":\"LED\",\"color\":\"red\",\"blink_hz\":2}";
  r = p.decode(led, strlen(led));
  Check(r.verdict == mechadog::Verdict::Accept, "LED 수락", r.reason);
  Check(strcmp(r.command.color, "red") == 0, "LED color 문자열", r.command.color);
  Check(r.command.blink_hz == 2.0F, "LED blink_hz", nullptr);

  const char* st = "{\"seq\":3,\"ts\":1,\"type\":\"STATE\",\"state\":\"HAZARD_DISPATCH\"}";
  r = p.decode(st, strlen(st));
  Check(r.verdict == mechadog::Verdict::Accept, "STATE 수락", r.reason);
  Check(r.command.state == mechadog::FsmState::HazardDispatch, "가장 긴 상태값 파싱", nullptr);
}

// ── 규칙 ② 클램핑은 폐기가 아니다 ───────────────────────────
void TestClamping() {
  mechadog::CommandParser p;
  const char* over = "{\"seq\":1,\"ts\":1,\"type\":\"MOVE\",\"step\":500,\"angle\":-90}";
  mechadog::DecodeResult r = p.decode(over, strlen(over));
  Check(r.verdict == mechadog::Verdict::Accept, "범위 초과는 폐기가 아니라 수락", r.reason);
  Check(r.command.step == 100.0F, "step 500 → 100", nullptr);
  Check(r.command.angle == -30.0F, "angle -90 → -30", nullptr);
  Check((r.command.clamped & mechadog::kClampStep) != 0, "step 클램핑 기록", nullptr);
  Check((r.command.clamped & mechadog::kClampAngle) != 0, "angle 클램핑 기록", nullptr);

  const char* act = "{\"seq\":2,\"ts\":1,\"type\":\"ACTION\",\"id\":99}";
  r = p.decode(act, strlen(act));
  Check(r.verdict == mechadog::Verdict::Accept, "ACTION 수락", r.reason);
  Check(r.command.action_id == 15, "id 99 → 15", nullptr);
}

// ── 규칙 ① seq 게이트 ────────────────────────────────────────
void TestSeqGate() {
  mechadog::CommandParser p;
  const char* a = "{\"seq\":5,\"ts\":1,\"type\":\"STOP\"}";
  const char* same = "{\"seq\":5,\"ts\":2,\"type\":\"STOP\"}";
  const char* older = "{\"seq\":4,\"ts\":3,\"type\":\"STOP\"}";
  const char* newer = "{\"seq\":6,\"ts\":4,\"type\":\"STOP\"}";

  Check(p.decode(a, strlen(a)).verdict == mechadog::Verdict::Accept, "첫 seq 수락", nullptr);
  Check(p.decode(same, strlen(same)).verdict == mechadog::Verdict::Discard, "중복 seq 폐기",
        nullptr);
  Check(p.decode(older, strlen(older)).verdict == mechadog::Verdict::Discard, "역전 seq 폐기",
        nullptr);
  Check(p.decode(newer, strlen(newer)).verdict == mechadog::Verdict::Accept, "증가 seq 수락",
        nullptr);

  int64_t last = 0;
  Check(p.last_seq(&last) && last == 6, "last_seq 노출", nullptr);

  // ⚠️ 참조 구현 `_SeqGate.admit` 은 통과시키는 즉시 갱신한다. 뒤에서 폐기되는
  //    미지 타입도 seq 를 전진시킨다 — 두 구현이 갈리기 쉬운 지점이라 못박는다.
  const char* unknown = "{\"seq\":7,\"ts\":5,\"type\":\"FUTURE_CMD\"}";
  Check(p.decode(unknown, strlen(unknown)).verdict == mechadog::Verdict::DiscardWarn,
        "미지 타입 = 폐기 + WARN", nullptr);
  Check(p.last_seq(&last) && last == 7, "미지 타입도 게이트를 전진시킨다", nullptr);

  // 링크 재수립 — 호스트가 재시작하면 seq 가 1 부터 다시 온다.
  p.reset();
  const char* restart = "{\"seq\":1,\"ts\":6,\"type\":\"STOP\"}";
  Check(p.decode(restart, strlen(restart)).verdict == mechadog::Verdict::Accept,
        "reset 후 낮은 seq 수락", nullptr);
}

// ── 규칙 ⑤ 종류 불일치는 조용히 폐기 (WARN 아님) ────────────
void TestTypeSafety() {
  struct Case {
    const char* raw;
    const char* what;
  };
  const Case cases[] = {
      {"{\"seq\":1,\"ts\":1,\"type\":[]}", "type 이 배열"},
      {"{\"seq\":1,\"ts\":1,\"type\":{}}", "type 이 객체"},
      {"{\"seq\":1,\"ts\":1,\"type\":5}", "type 이 숫자"},
      {"{\"seq\":1.5,\"ts\":1,\"type\":\"STOP\"}", "seq 가 실수"},
      {"{\"seq\":true,\"ts\":1,\"type\":\"STOP\"}", "seq 가 불리언"},
      {"{\"seq\":null,\"ts\":1,\"type\":\"STOP\"}", "seq 가 null"},
      {"{\"seq\":1,\"ts\":1.5,\"type\":\"STOP\"}", "ts 가 실수"},
      {"{\"seq\":1,\"ts\":1,\"type\":\"MOVE\",\"step\":\"60\",\"angle\":0}", "step 이 문자열"},
      {"{\"seq\":1,\"ts\":1,\"type\":\"LED\",\"color\":7,\"blink_hz\":1}", "color 가 숫자"},
  };
  for (const Case& c : cases) {
    mechadog::CommandParser p;
    const mechadog::DecodeResult r = p.decode(c.raw, strlen(c.raw));
    Check(r.verdict == mechadog::Verdict::Discard, c.what, r.reason);
  }
}

// ── 크래시 금지 ──────────────────────────────────────────────
// UDP 로는 무엇이든 들어온다. 수신 루프가 죽으면 관제가 멈추므로 폐기보다 나쁘다.
void TestMalformedNeverCrashes() {
  static const char kDeep[] =
      "{\"a\":[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[";
  static const char kBinary[] = {'{', '\x00', '\xff', '\xfe', ':', '\x01', '}'};

  struct Case {
    const char* raw;
    size_t len;
    const char* what;
  };
  const Case cases[] = {
      {"", 0, "빈 입력"},
      {"{", 1, "여는 괄호만"},
      {"}", 1, "닫는 괄호만"},
      {"{\"seq\":1", 8, "중간에서 끊김"},
      {"{\"seq\"", 6, "키 뒤에서 끊김"},
      {"{\"unterminated", 14, "닫히지 않은 문자열"},
      {"not json at all", 15, "JSON 이 아님"},
      {"[1,2,3]", 7, "최상위가 배열"},
      {"null", 4, "최상위가 null"},
      {"{}", 2, "빈 객체"},
      {"{\"seq\":99999999999999999999999,\"ts\":1,\"type\":\"STOP\"}", 51, "seq 가 2^53 초과"},
      {"{\"seq\":1,\"ts\":1,\"type\":\"STOP\",\"x\":{\"y\":{\"z\":1}}}", 47, "중첩 객체 건너뛰기"},
      {kDeep, sizeof(kDeep) - 1, "과도한 중첩"},
      {kBinary, sizeof(kBinary), "널 바이트 포함 이진 쓰레기"},
  };
  for (const Case& c : cases) {
    mechadog::CommandParser p;
    const mechadog::DecodeResult r = p.decode(c.raw, c.len);
    // 판정이 무엇인지는 상관없다. 돌아오기만 하면 통과다. 다만 사유 문자열은
    // 로그로 나가므로 널이면 안 된다 — 그것까지 함께 못박는다.
    Check(r.reason != nullptr, c.what, "reason 이 널");
  }

  // 널 종단이 없는 버퍼 — len 만이 경계다. 파서가 그 너머를 읽으면 여기서 드러난다.
  const char raw[] = {'{', '"', 's', 'e', 'q', '"', ':', '1', '}', 'G', 'A', 'R', 'B'};
  mechadog::CommandParser p;
  const mechadog::DecodeResult r = p.decode(raw, 9);
  Check(r.verdict == mechadog::Verdict::Discard, "널 종단 없는 버퍼 (ts·type 누락)", r.reason);
}

// ── 모르는 상태는 WARN, 기형은 조용히 ───────────────────────
void TestUnknownStateIsWarn() {
  mechadog::CommandParser p;
  const char* dancing = "{\"seq\":1,\"ts\":1,\"type\":\"STATE\",\"state\":\"DANCING\"}";
  const mechadog::DecodeResult r = p.decode(dancing, strlen(dancing));
  Check(r.verdict == mechadog::Verdict::DiscardWarn, "미지 상태 = 폐기 + WARN", r.reason);
}

}  // namespace

int main(int argc, char** argv) {
  const char* dir = (argc > 1) ? argv[1] : "tests/fixtures";
  printf("픽스처 경로: %s\n\n", dir);

  const int ok_lines = RunFixture(dir, "protocol_samples.jsonl", true);
  printf("protocol_samples.jsonl  %d 줄\n", ok_lines);
  const int bad_lines = RunFixture(dir, "protocol_invalid.jsonl", false);
  printf("protocol_invalid.jsonl  %d 줄\n", bad_lines);

  // 픽스처가 사라지거나 비면 조용히 통과해 버린다. 그것부터 막는다.
  Check(ok_lines >= 15, "정본 픽스처가 8종 전부를 덮는다 (15줄 이상)", nullptr);
  Check(bad_lines >= 6, "이상 픽스처가 6줄 이상", nullptr);

  TestFieldValues();
  TestClamping();
  TestSeqGate();
  TestTypeSafety();
  TestMalformedNeverCrashes();
  TestUnknownStateIsWarn();

  printf("\n검사 %d건 · 실패 %d건\n", g_checks, g_failures);
  if (g_failures != 0) {
    printf("FAILED\n");
    return 1;
  }
  printf("PASSED\n");
  return 0;
}
