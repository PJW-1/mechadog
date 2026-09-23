// WBS 4.7.20: 로봇 MP3 모듈(IIC1 0x7B)의 재생 순서. 하드웨어 무관 — 버스 쓰기는
// 부르는 쪽이 넘기고(sensor_hal 의 writeMp3Volume·writeMp3Track), 이 헤더는 «언제
// 무엇을 쓰는가» 만 정한다. 시험은 test/test_mp3_player.cpp.
#ifndef MECHADOG_MP3_PLAYER_H
#define MECHADOG_MP3_PLAYER_H

#include <stdint.h>

// 0~30. 현장 보정 손잡이다 — 빌드 플래그로 바꾼다.
#ifndef MECHADOG_MP3_VOLUME
#define MECHADOG_MP3_VOLUME 20
#endif

namespace mechadog {

// 트랙 레지스터가 받는 범위(벤더 헤더 «0~3000»). 0 은 재생이 아니라 정지로 쓴다.
constexpr int32_t kMp3TrackMax = 3000;
// 벤더 드라이버는 명령마다 delay(20) 을 둔다. 모듈이 앞 명령을 삼킬 시간으로 보고
// 같은 간격을 지키되, 루프를 멈추지 않고 다음 회전으로 넘긴다.
constexpr uint32_t kMp3WriteGapMs = 20;
// 모듈이 없거나 버스를 못 얻을 때 시도하는 횟수. 경고는 사건이라 늦게 나가면 틀린
// 경고가 된다 — 끝없이 붙잡지 않고 버린다.
constexpr uint8_t kMp3MaxAttempts = 5;

enum class Mp3Step : uint8_t { Idle, Waiting, Wrote, Played, Retry, Dropped };

struct Mp3Player {
  int32_t track = -1;  // 대기 중인 트랙. -1 이면 없음.
  bool track_sent = false;
  uint8_t failures = 0;
  uint32_t last_write_ms = 0;

  // 나중 요청이 이긴다. 범위 밖은 받지 않는다 — 잘라 받으면 다른 문장이 나간다.
  bool request(int32_t wanted) {
    if (wanted < 0 || wanted > kMp3TrackMax) return false;
    track = wanted;
    track_sent = false;
    failures = 0;
    return true;
  }

  // 한 회전에 버스 쓰기 한 번. 트랙을 쓰고 간격 뒤에 음량을 쓴다 — 모듈은 재생 중에
  // 받은 음량만 반영하고, 재생 전에 보낸 음량은 버린다(2026-09-24 실측). 재생마다
  // 다시 쓰므로 모듈이 전원 흔들림으로 초기화돼도 다음 재생에서 돌아온다.
  // 정지(0)는 음량 없이 한 번으로 끝난다.
  template <typename WriteVolume, typename WriteTrack>
  Mp3Step poll(uint32_t now_ms, WriteVolume write_volume, WriteTrack write_track) {
    if (track < 0) return Mp3Step::Idle;
    if (now_ms - last_write_ms < kMp3WriteGapMs) return Mp3Step::Waiting;
    last_write_ms = now_ms;
    const bool ok = track_sent ? write_volume() : write_track(track);
    if (!ok) {
      if (++failures < kMp3MaxAttempts) return Mp3Step::Retry;
      track = -1;
      return Mp3Step::Dropped;
    }
    failures = 0;
    if (!track_sent && track != 0) {
      track_sent = true;
      return Mp3Step::Wrote;
    }
    track = -1;
    return Mp3Step::Played;
  }
};

}  // namespace mechadog

#endif  // MECHADOG_MP3_PLAYER_H
