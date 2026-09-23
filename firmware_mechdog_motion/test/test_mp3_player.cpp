#include <cstdint>
#include <cstdio>
#include <cstdlib>

#include "mp3_player.h"

namespace {
int checks = 0;

void check(bool condition) {
  ++checks;
  if (!condition) {
    std::fprintf(stderr, "MP3 player check %d failed\n", checks);
    std::abort();
  }
}

// 버스 쓰기를 흉내 낸다. 무엇을 몇 번 썼는지 남긴다.
struct FakeBus {
  bool ok = true;
  int volumes = 0;
  int plays = 0;
  int32_t last_track = -1;
};
}  // namespace

int main() {
  using mechadog::Mp3Player;
  using mechadog::Mp3Step;
  FakeBus bus;
  auto volume = [&bus]() {
    ++bus.volumes;
    return bus.ok;
  };
  auto play = [&bus](int32_t track) {
    ++bus.plays;
    bus.last_track = track;
    return bus.ok;
  };

  // 범위: 0(정지)~3000. 밖은 받지 않는다 — 잘라 받으면 다른 문장이 나간다.
  Mp3Player player;
  check(!player.request(-1));
  check(!player.request(mechadog::kMp3TrackMax + 1));
  check(player.poll(1000, volume, play) == Mp3Step::Idle);
  check(bus.volumes == 0 && bus.plays == 0);

  // 트랙 → 간격 → 음량. 한 회전에 한 번만 쓴다. 모듈은 재생 중에 받은 음량만
  // 반영한다(2026-09-24 실측) — 그래서 음량이 트랙 뒤에 간다.
  check(player.request(17));
  check(player.poll(1000, volume, play) == Mp3Step::Wrote);
  check(bus.plays == 1 && bus.last_track == 17 && bus.volumes == 0);
  check(player.poll(1000 + mechadog::kMp3WriteGapMs - 1, volume, play) == Mp3Step::Waiting);
  check(bus.volumes == 0);
  check(player.poll(1000 + mechadog::kMp3WriteGapMs, volume, play) == Mp3Step::Played);
  check(bus.volumes == 1 && bus.plays == 1);
  check(player.poll(2000, volume, play) == Mp3Step::Idle);

  // 정지(0)는 한 번 쓰고 끝난다. 멈춘 모듈에는 음량이 먹지 않는다.
  check(player.request(0));
  check(player.poll(3000, volume, play) == Mp3Step::Played);
  check(bus.last_track == 0 && bus.volumes == 1);
  check(player.poll(3000 + mechadog::kMp3WriteGapMs, volume, play) == Mp3Step::Idle);

  // 나중 요청이 이긴다. 트랙부터 다시 쓴다.
  check(player.request(5));
  check(player.poll(4000, volume, play) == Mp3Step::Wrote);
  check(player.request(6));
  const int volumes_before = bus.volumes;
  check(player.poll(4000 + mechadog::kMp3WriteGapMs, volume, play) == Mp3Step::Wrote);
  check(bus.last_track == 6 && bus.volumes == volumes_before);
  check(player.poll(4000 + 2 * mechadog::kMp3WriteGapMs, volume, play) == Mp3Step::Played);
  check(bus.volumes == volumes_before + 1);

  // 모듈이 없으면 정해진 횟수만 시도하고 버린다. 늦게 나간 경고는 틀린 경고다.
  bus.ok = false;
  check(player.request(9));
  uint32_t now = 5000;
  for (uint8_t attempt = 1; attempt < mechadog::kMp3MaxAttempts; ++attempt) {
    check(player.poll(now, volume, play) == Mp3Step::Retry);
    now += mechadog::kMp3WriteGapMs;
  }
  check(player.poll(now, volume, play) == Mp3Step::Dropped);
  check(player.poll(now + 1000, volume, play) == Mp3Step::Idle);

  // 버린 뒤 새 요청은 실패 횟수를 새로 센다.
  bus.ok = true;
  check(player.request(3));
  check(player.poll(now + 2000, volume, play) == Mp3Step::Wrote);

  // millis() 가 한 바퀴 돌아도 간격 계산이 맞다.
  Mp3Player wrap;
  check(wrap.request(1));
  check(wrap.poll(UINT32_MAX - 5, volume, play) == Mp3Step::Wrote);
  check(wrap.poll(UINT32_MAX, volume, play) == Mp3Step::Waiting);
  check(wrap.poll(mechadog::kMp3WriteGapMs - 6, volume, play) == Mp3Step::Played);

  std::printf("MP3 player checks passed: %d\n", checks);
  return 0;
}
