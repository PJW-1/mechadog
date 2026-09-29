#include "ld19.h"

#include <string.h>

namespace mechadog {

uint8_t Ld19Crc8(const uint8_t* data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x4D) : static_cast<uint8_t>(crc << 1);
    }
  }
  return crc;
}

namespace {

uint16_t ReadLe16(const uint8_t* p) {
  return static_cast<uint16_t>(p[0] | (static_cast<uint16_t>(p[1]) << 8));
}

}  // namespace

bool Ld19Parser::feed(uint8_t byte) {
  if (len_ == 0 && byte != kLd19Header) {
    return false;  // 헤더를 찾는 중 — 스트림 쓰레기를 버린다
  }
  if (len_ >= kLd19FrameBytes) {
    resync_();  // 이론상 도달 불가지만 방어한다
  }
  buf_[len_++] = byte;
  if (len_ < kLd19FrameBytes) {
    return false;
  }

  // 47바이트가 모였다 — 검증한다
  if (buf_[1] != kLd19VerLen) {
    ++bad_verlen_;
    resync_();
    return false;
  }
  if (Ld19Crc8(buf_, kLd19FrameBytes - 1) != buf_[kLd19FrameBytes - 1]) {
    ++crc_failures_;
    resync_();
    return false;
  }

  Ld19Frame& f = frame_;
  f.speed_dps = ReadLe16(&buf_[2]);
  f.start_cdeg = ReadLe16(&buf_[4]);
  for (size_t i = 0; i < kLd19PointsPerFrame; ++i) {
    const uint8_t* p = &buf_[6 + i * 3];
    f.points[i].dist_mm = ReadLe16(p);
    f.points[i].intensity = p[2];
  }
  f.end_cdeg = ReadLe16(&buf_[42]);
  f.stamp_ms = ReadLe16(&buf_[44]);

  ++frames_ok_;
  len_ = 0;
  return true;
}

void Ld19Parser::resync_() {
  // 버퍼 안에 다음 프레임의 헤더가 이미 들어와 있을 수 있다 —
  // 첫 0x54 부터 남은 바이트를 앞으로 당긴다.
  ++resyncs_;
  for (size_t i = 1; i < len_; ++i) {
    if (buf_[i] == kLd19Header) {
      memmove(buf_, &buf_[i], len_ - i);
      len_ -= i;
      return;
    }
  }
  len_ = 0;
}

size_t ScanAssembler::addFrame(const Ld19Frame& f, ScanPoint* out, size_t out_capacity) {
  // 새 회전 감지: 회전 내에서는 start 각도가 단조 증가한다.
  // 작아지면 0° 를 넘은 것 — 모아 둔 점들이 완성된 한 바퀴다.
  size_t emitted = 0;
  if (have_last_ && f.start_cdeg < last_start_cdeg_) {
    emitted = count_ < out_capacity ? count_ : out_capacity;
    memcpy(out, points_, emitted * sizeof(ScanPoint));
    ++scans_completed_;
    count_ = 0;
  }
  last_start_cdeg_ = f.start_cdeg;
  have_last_ = true;

  // 이 프레임이 0° 를 걸치면 end 를 한 바퀴 앞으로 밀어 보간한다
  int32_t end = f.end_cdeg;
  if (end < f.start_cdeg) {
    end += kCdegPerRevolution;
  }
  // 12점은 [start, end] 구간을 11등분한 위치에 있다
  const int32_t span = end - f.start_cdeg;

  for (size_t i = 0; i < kLd19PointsPerFrame; ++i) {
    if (count_ >= kMaxScanPoints) {
      ++points_dropped_;
      continue;
    }
    ScanPoint& p = points_[count_++];
    p.angle_cdeg = static_cast<uint16_t>((f.start_cdeg + (span * static_cast<int32_t>(i)) / 11) %
                                         kCdegPerRevolution);
    p.dist_mm = f.points[i].dist_mm;
    p.quality = f.points[i].intensity;
  }
  return emitted;
}

size_t ScanAssembler::flush(ScanPoint* out, size_t out_capacity) {
  const size_t n = count_ < out_capacity ? count_ : out_capacity;
  memcpy(out, points_, n * sizeof(ScanPoint));
  count_ = 0;
  return n;
}

}  // namespace mechadog
