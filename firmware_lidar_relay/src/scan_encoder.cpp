#include "scan_encoder.h"

#include <stdio.h>
#include <string.h>

namespace mechadog {

bool ScanEncoder::begin(const char* device_id, const char* boot_id, int64_t start_seq) {
  configured_ = false;
  if (device_id == nullptr || boot_id == nullptr || start_seq < 1) {
    return false;
  }
  const size_t dlen = strlen(device_id);
  const size_t blen = strlen(boot_id);
  if (dlen == 0 || dlen > kMaxIdentityBytes || blen == 0 || blen > kMaxIdentityBytes) {
    return false;
  }
  memcpy(device_id_, device_id, dlen + 1);
  memcpy(boot_id_, boot_id, blen + 1);
  next_seq_ = start_seq;
  configured_ = true;
  return true;
}

ScanEncodeResult ScanEncoder::encode(const ScanPoint* points, size_t count, int64_t ts_ms,
                                     char* out, size_t capacity) {
  if (out == nullptr || capacity == 0) {
    return ScanEncodeResult(false, 0, "출력 버퍼 없음");
  }
  out[0] = '\0';
  if (!configured_) {
    return ScanEncodeResult(false, 0, "begin() 미호출");
  }
  if (points == nullptr && count > 0) {
    return ScanEncodeResult(false, 0, "points 가 nullptr");
  }
  if (ts_ms < 0) {
    return ScanEncodeResult(false, 0, "ts 가 음수");
  }

  int n = snprintf(out, capacity,
                   "{\"seq\":%lld,\"ts\":%lld,\"type\":\"SCAN\",\"device_id\":"
                   "\"%s\",\"boot_id\":\"%s\",\"points\":[",
                   static_cast<long long>(next_seq_), static_cast<long long>(ts_ms), device_id_,
                   boot_id_);
  if (n < 0 || static_cast<size_t>(n) >= capacity) {
    return ScanEncodeResult(false, 0, "헤더가 버퍼를 초과");
  }
  size_t used = static_cast<size_t>(n);

  for (size_t i = 0; i < count; ++i) {
    // 각도는 소수 둘째자리 고정(cdeg 그대로) — %f 없이 정수만으로 출력한다
    char point[20];
    const size_t plen =
        static_cast<size_t>(snprintf(point, sizeof(point), "%s[%u.%02u,%u]", i == 0 ? "" : ",",
                                     static_cast<unsigned>(points[i].angle_cdeg / 100),
                                     static_cast<unsigned>(points[i].angle_cdeg % 100),
                                     static_cast<unsigned>(points[i].dist_mm)));
    if (plen == 0 || used + plen + 2 >= capacity) {
      return ScanEncodeResult(false, 0, "점 직렬화가 버퍼를 초과");
    }
    memcpy(out + used, point, plen);
    used += plen;
  }
  if (used + 2 > capacity) {
    return ScanEncodeResult(false, 0, "꼬리가 버퍼를 초과");
  }
  out[used++] = ']';
  out[used++] = '}';
  out[used] = '\0';

  ++next_seq_;
  return ScanEncodeResult(true, used, "");
}

}  // namespace mechadog
