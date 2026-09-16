#include "stationary_ota.h"

#include "ota_health.h"
#include "reboot_deadline.h"
#include "sensor_hal.h"

#if MECHADOG_ENABLE_OTA
#include <Arduino.h>
#include <Wire.h>
#include <esp_app_format.h>
#include <esp_https_server.h>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <mbedtls/sha256.h>

#include <algorithm>
#include <atomic>
#include <cstring>

#include "motion_hal.h"
#if MECHADOG_ENABLE_TASK_WDT
#include "task_watchdog.h"
#endif

// Actuator builds carry OTA only when the runtime SERVICE mode exists to park
// the body first (task-watchdog expansion builds). Without it there is no
// stationary guarantee, so moving maintenance stays unintegrated.
#if MECHADOG_ENABLE_ACTUATORS && !MECHADOG_ENABLE_TASK_WDT
#error "Stationary OTA requires actuators OFF or the SERVICE-mode watchdog build"
#endif
#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
#error "OTA requires the SDK bootloader rollback contract"
#endif
#ifndef MECHADOG_OTA_TOKEN
#error "Provide a private OTA token and TLS certificate/key outside the repository"
#endif

// Arduino normally confirms an image before setup(); defer to live health + PC.
extern "C" bool verifyRollbackLater() {
  return true;
}

namespace {
constexpr uint32_t kSlotSize = 0xf0000;
constexpr uint64_t kVerifyDeadlineUs = 90000000;
constexpr uint64_t kTransferDeadlineUs = 120000000;
constexpr char kToken[] = MECHADOG_OTA_TOKEN;
constexpr char kCertificate[] = MECHADOG_OTA_CERT;
constexpr char kPrivateKey[] = MECHADOG_OTA_KEY;
std::atomic<bool> g_started{false};
std::atomic<bool> g_start_requested{false};
std::atomic<bool> g_healthy{false};
std::atomic<bool> g_confirm_requested{false};
std::atomic<bool> g_confirmed{false};
std::atomic<bool> g_updating{false};
mechadog::RebootDeadline g_reboot;
mechadog::OtaHealth g_health;
esp_timer_handle_t g_verify_timer = nullptr;
httpd_handle_t g_server = nullptr;
char g_mac[13] = {};
char g_boot[17] = {};
char g_image_sha[65] = {};
bool g_pending = false;

void toHex(const uint8_t* in, size_t size, char* out) {
  constexpr char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < size; ++i) {
    out[2 * i] = digits[in[i] >> 4];
    out[2 * i + 1] = digits[in[i] & 15];
  }
  out[2 * size] = 0;
}

bool equalSecret(const char* a, const char* b, size_t size) {
  unsigned difference = 0;
  for (size_t i = 0; i < size; ++i) difference |= a[i] ^ b[i];
  return difference == 0;
}

esp_err_t reply(httpd_req_t* req, const char* status, const char* body) {
  httpd_resp_set_status(req, status);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  httpd_resp_set_hdr(req, "Connection", "close");
  return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

bool authorized(httpd_req_t* req) {
  char value[80] = {};
  const size_t n = httpd_req_get_hdr_value_len(req, "Authorization");
  return n == 7 + strlen(kToken) && n < sizeof(value) &&
         httpd_req_get_hdr_value_str(req, "Authorization", value, sizeof(value)) == ESP_OK &&
         strncmp(value, "Bearer ", 7) == 0 && equalSecret(value + 7, kToken, strlen(kToken));
}

void rebootDeadline(void*) {
  // A pending image has not proved both sensor/loop health and PC reachability.
  // The standard bootloader will roll it back on this restart.
  esp_restart();
}

// I2C 버스에 무엇이 달려 있는지 부팅 시 한 번만 읽어 둔다 (WBS 2.1.2).
//
// **왜 부팅 시점인가.** 부팅 스냅샷은 센서 태스크가 생성되기 전에 한 번만 돌아
// 경쟁이 없다. 요청 시점 재스캔은 /i2c/live 가 mechadog::lockI2cBus() 로 센서
// 태스크와 트랜잭션을 상호배제한다 — HTTP 핸들러에서 같은 버스를 건드리는 동안
// 초음파·IMU 읽기와 시퀀스가 섞이지 않게 한다.
//
// 주소만으로 부품을 확정할 수는 없다. 알려진 후보를 함께 적고 판단은 사람이 한다.
// 주의: WonderEcho 의 0x64 는 장치 주소가 아니라 0x34 슬레이브 안의 "인식 결과"
// 레지스터다 (공식 I2C 프로토콜 문서). 외부 버스에서 응답해야 할 주소는 0x34 이다.
constexpr int kBus1Sda = 22;  // IIC1 — sensor_hal.cpp 의 kSdaPin/kSclPin 과 같아야 한다
constexpr int kBus1Scl = 23;
constexpr int kBus2Sda = 19;  // IIC2 — 공식 IoT 레슨이 Wi-Fi 모듈(0x69)에 쓰는 버스
constexpr int kBus2Scl = 13;

char g_i2c_scan[320] = "{\"scanned\":false}";

void appendFound(TwoWire& bus, int sda, int scl, char* out, size_t cap, size_t& used) {
  bus.begin(sda, scl, 100000);
  // 이 스캔은 setup() 안에서 돈다. 기본 50 ms 타임아웃이면 빈 버스 한 개만 해도
  // 112 x 50 ms = 5.6 초를 잡아먹고, 슬레이브가 SDA 를 붙들면 더 길어진다.
  // OTA 로 올린 이미지는 90 초 안에 /confirm 을 받아야 하므로 부팅이 늦어지면
  // 그대로 롤백된다 - 실제로 이 스캔 때문에 확정 창을 놓친 적이 있다.
  // 주소당 5 ms 로 묶으면 두 버스 최악이 약 1.1 초다.
  bus.setTimeOut(5);
  bool first = true;
  for (uint8_t addr = 0x08; addr < 0x78; ++addr) {
    bus.beginTransmission(addr);
    if (bus.endTransmission() != 0) continue;
    const int written = snprintf(out + used, cap - used, "%s\"0x%02X\"", first ? "" : ",", addr);
    if (written <= 0 || static_cast<size_t>(written) >= cap - used) return;
    used += static_cast<size_t>(written);
    first = false;
  }
  // SensorHal 이 곧 같은 Wire 를 쓴다. 짧은 타임아웃을 남겨두지 않는다.
  bus.setTimeOut(50);
}

void scanI2cOnce() {
  size_t used = 0;
  const size_t cap = sizeof(g_i2c_scan);
  used += static_cast<size_t>(snprintf(g_i2c_scan, cap, "{\"scanned\":true,\"iic1\":["));
  appendFound(Wire, kBus1Sda, kBus1Scl, g_i2c_scan, cap, used);
  used += static_cast<size_t>(snprintf(g_i2c_scan + used, cap - used, "],\"iic2\":["));
  appendFound(Wire1, kBus2Sda, kBus2Scl, g_i2c_scan, cap, used);
  snprintf(g_i2c_scan + used, cap - used,
           "],\"known\":{\"0x34\":\"WonderEcho i2c\",\"0x69\":\"wifi\","
           "\"0x6A\":\"QMI8658 imu\",\"0x77\":\"sonar\"}}");
}

esp_err_t i2cHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  return reply(req, "200 OK", g_i2c_scan);
}

// ---- 요청 시점 라이브 진단 ----
//
// 부팅 스냅샷과 달리 핸들러가 돌 때 버스를 다시 훑는다. 모든 Wire 접근은
// mechadog::lockI2cBus() 를 잡고 주소마다 푼다 — 한 번에 오래 잠그면 센서
// 태스크가 40 ms 주기를 넘겨 CycleElapsed 로 IMU 가 재부팅까지 멈춘다.
// 단일 주소 프로브는 /i2c/live?bus=1&from=0x64&to=0x64 처럼 범위를 좁혀 쓴다.

// 쿼리 값 파싱: 없으면 기본값 유지(true), 있지만 형식이 나쁘면 false.
// "0x64" 와 "100" 둘 다 받는다.
bool queryByte(const char* query, const char* key, uint8_t& out, bool& bad) {
  char value[8] = {};
  if (httpd_query_key_value(query, key, value, sizeof(value)) != ESP_OK) return true;
  char* end = nullptr;
  const long parsed = strtol(value, &end, 0);
  if (end == value || *end != '\0' || parsed < 0 || parsed > 0xFF) {
    bad = true;
    return false;
  }
  out = static_cast<uint8_t>(parsed);
  return true;
}

bool queryHas(const char* query, const char* key) {
  char value[8];
  return httpd_query_key_value(query, key, value, sizeof(value)) == ESP_OK;
}

size_t liveScanBus(TwoWire& bus, uint8_t lo, uint8_t hi, char* out, size_t cap, size_t used) {
  const uint16_t saved_timeout = bus.getTimeOut();
  bus.setTimeOut(5);  // 멈춘 슬레이브가 SDA 를 붙들어도 주소당 5 ms 에서 끊는다.
  bool first = true;
  for (uint16_t addr = lo; addr <= hi; ++addr) {
    // 잠금 실패는 건너뛴다: 진단 요청이 센서 태스크를 기다리며 멈추지 않게.
    if (!mechadog::lockI2cBus(20)) continue;
    bus.beginTransmission(static_cast<uint8_t>(addr));
    const uint8_t result = bus.endTransmission();
    mechadog::unlockI2cBus();
    if (result != 0) continue;
    const int written = snprintf(out + used, cap - used, "%s\"0x%02X\"", first ? "" : ",",
                                 static_cast<unsigned>(addr));
    if (written <= 0 || static_cast<size_t>(written) >= cap - used) break;
    used += static_cast<size_t>(written);
    first = false;
  }
  bus.setTimeOut(saved_timeout);
  return used;
}

esp_err_t i2cLiveHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  char query[96] = {};
  const size_t qlen = httpd_req_get_url_query_len(req);
  if (qlen >= sizeof(query) ||
      (qlen && httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK))
    return reply(req, "400 Bad Request", "{\"error\":\"query\"}");
  uint8_t lo = 0x08, hi = 0x77, bus_no = 0;
  bool bad = false;
  if (!queryByte(query, "from", lo, bad) || !queryByte(query, "to", hi, bad) ||
      !queryByte(query, "bus", bus_no, bad) || bad || bus_no > 2)
    return reply(req, "400 Bad Request", "{\"error\":\"param\"}");
  if (lo < 0x08) lo = 0x08;  // 예약 영역(0x00-0x07)은 건드리지 않는다.
  if (hi > 0x77) hi = 0x77;
  if (hi < lo) hi = lo;
  const int64_t started = esp_timer_get_time();
  char body[384];
  size_t used = static_cast<size_t>(
      snprintf(body, sizeof(body), "{\"live\":true,\"uptime_ms\":%lu",
               static_cast<unsigned long>(millis())));
  if (bus_no <= 1) {
    used += static_cast<size_t>(snprintf(body + used, sizeof(body) - used, ",\"iic1\":["));
    used = liveScanBus(Wire, lo, hi, body, sizeof(body), used);
    used += static_cast<size_t>(snprintf(body + used, sizeof(body) - used, "]"));
  }
  if (bus_no == 0 || bus_no == 2) {
    used += static_cast<size_t>(snprintf(body + used, sizeof(body) - used, ",\"iic2\":["));
    used = liveScanBus(Wire1, lo, hi, body, sizeof(body), used);
    used += static_cast<size_t>(snprintf(body + used, sizeof(body) - used, "]"));
  }
  snprintf(body + used, sizeof(body) - used, ",\"elapsed_ms\":%lu}",
           static_cast<unsigned long>((esp_timer_get_time() - started) / 1000));
  return reply(req, "200 OK", body);
}

// 레지스터 읽기: /i2c/read?addr=0x34&reg=0x64&n=1&stop=0&bus=1
// stop=0 은 IMU 방식(레지스터 쓰고 repeated START), 1 은 초음파 방식
// (STOP 후 새 read). 벤더마다 요구 시퀀스가 달라 둘 다 지원한다.
esp_err_t i2cReadHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  char query[96] = {};
  const size_t qlen = httpd_req_get_url_query_len(req);
  if (qlen >= sizeof(query) || qlen == 0 ||
      httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK)
    return reply(req, "400 Bad Request", "{\"error\":\"query\"}");
  uint8_t addr = 0, reg = 0, n = 1, stop = 0, bus_no = 1;
  bool bad = false;
  if (!queryHas(query, "addr") || !queryHas(query, "reg"))
    return reply(req, "400 Bad Request", "{\"error\":\"param_missing\"}");
  if (!queryByte(query, "addr", addr, bad) || !queryByte(query, "reg", reg, bad) ||
      !queryByte(query, "n", n, bad) || !queryByte(query, "stop", stop, bad) ||
      !queryByte(query, "bus", bus_no, bad) || bad || n < 1 || n > 16 || stop > 1 ||
      bus_no < 1 || bus_no > 2 || addr < 0x08 || addr > 0x77)
    return reply(req, "400 Bad Request", "{\"error\":\"param\"}");
  TwoWire& bus = bus_no == 2 ? Wire1 : Wire;
  uint8_t buf[16] = {};
  const int64_t started = esp_timer_get_time();
  if (!mechadog::lockI2cBus(50))
    return reply(req, "503 Service Unavailable", "{\"error\":\"bus_busy\"}");
  const uint16_t saved_timeout = bus.getTimeOut();
  bus.setTimeOut(5);
  bus.beginTransmission(addr);
  const bool wrote = bus.write(reg) == 1;
  const uint8_t tx = bus.endTransmission(stop ? true : false);
  size_t got = 0;
  if (wrote && tx == 0) {
    got = bus.requestFrom(addr, n, true);
    for (size_t i = 0; i < got && i < sizeof(buf); ++i) {
      const int b = bus.read();
      if (b < 0) break;
      buf[i] = static_cast<uint8_t>(b);
    }
  }
  bus.setTimeOut(saved_timeout);
  mechadog::unlockI2cBus();
  char hex[33];
  toHex(buf, got, hex);
  char body[192];
  snprintf(body, sizeof(body),
           "{\"addr\":\"0x%02X\",\"reg\":\"0x%02X\",\"wrote\":%s,\"tx\":%u,"
           "\"got\":%u,\"bytes\":\"%s\",\"elapsed_ms\":%lu}",
           addr, reg, wrote ? "true" : "false", tx, static_cast<unsigned>(got), hex,
           static_cast<unsigned long>((esp_timer_get_time() - started) / 1000));
  return reply(req, "200 OK", body);
}

// 레지스터/데이터 쓰기: POST /i2c/write?addr=0x34&data=6e01
// data 는 16진 문자열(첫 바이트가 보통 레지스터). WonderEcho 의 0x6E 방송
// 트리거처럼 쓰기 동작을 실기 확인하는 용도다.
esp_err_t i2cWriteHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  char query[160] = {};
  const size_t qlen = httpd_req_get_url_query_len(req);
  if (qlen >= sizeof(query) || qlen == 0 ||
      httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK)
    return reply(req, "400 Bad Request", "{\"error\":\"query\"}");
  uint8_t addr = 0, bus_no = 1;
  bool bad = false;
  char hexdata[65] = {};
  if (!queryHas(query, "addr") ||
      httpd_query_key_value(query, "data", hexdata, sizeof(hexdata)) != ESP_OK ||
      !queryByte(query, "addr", addr, bad) ||
      !queryByte(query, "bus", bus_no, bad) || bad || bus_no < 1 || bus_no > 2 ||
      addr < 0x08 || addr > 0x77)
    return reply(req, "400 Bad Request", "{\"error\":\"param\"}");
  const size_t hexlen = strlen(hexdata);
  if (hexlen == 0 || hexlen % 2 != 0)
    return reply(req, "400 Bad Request", "{\"error\":\"data\"}");
  uint8_t bytes[32];
  const size_t count = hexlen / 2;
  for (size_t i = 0; i < count; ++i) {
    char pair[3] = {hexdata[2 * i], hexdata[2 * i + 1], 0};
    char* end = nullptr;
    const long v = strtol(pair, &end, 16);
    if (end != pair + 2) return reply(req, "400 Bad Request", "{\"error\":\"data\"}");
    bytes[i] = static_cast<uint8_t>(v);
  }
  TwoWire& bus = bus_no == 2 ? Wire1 : Wire;
  const int64_t started = esp_timer_get_time();
  if (!mechadog::lockI2cBus(50))
    return reply(req, "503 Service Unavailable", "{\"error\":\"bus_busy\"}");
  const uint16_t saved_timeout = bus.getTimeOut();
  bus.setTimeOut(5);
  bus.beginTransmission(addr);
  const size_t wrote = bus.write(bytes, count);
  const uint8_t tx = bus.endTransmission(true);
  bus.setTimeOut(saved_timeout);
  mechadog::unlockI2cBus();
  char body[160];
  snprintf(body, sizeof(body),
           "{\"addr\":\"0x%02X\",\"wrote\":%u,\"tx\":%u,\"elapsed_ms\":%lu}",
           addr, static_cast<unsigned>(wrote), tx,
           static_cast<unsigned long>((esp_timer_get_time() - started) / 1000));
  return reply(req, "200 OK", body);
}

// 주차·안전 상태에서만 받는 재부팅. pending 이미지 상태로 재부팅하면
// 부트로더가 롤백하므로 pending 여부를 응답에 포함한다.
esp_err_t rebootHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  if (g_updating) return reply(req, "409 Conflict", "{\"error\":\"updating\"}");
  if (!mechadog::otaParkedForReboot())
    return reply(req, "409 Conflict", "{\"error\":\"not_parked\"}");
  char body[128];
  snprintf(body, sizeof(body), "{\"rebooting\":true,\"pending_rollback\":%s}",
           (g_pending && !g_confirmed) ? "true" : "false");
  g_reboot.schedule(millis(), 500);
  return reply(req, "202 Accepted", body);
}

esp_err_t statusHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  const auto* running = esp_ota_get_running_partition();
  // 감시·고장주입 여부는 빌드 구성으로 고정된다. bool 로 두면 기본 빌드에서
  // 아래 삼항 조건이 항상 거짓이라 정적 분석이 죽은 분기로 잡는다.
#if MECHADOG_ENABLE_TASK_WDT
  const char* const watchdog_armed = mechadog::taskWatchdogArmed() ? "true" : "false";
  const unsigned watchdog_deadline_ms = mechadog::kLoopWatchdogDeadlineUs / 1000;
#else
  const char* const watchdog_armed = "false";
  const unsigned watchdog_deadline_ms = 0;
#endif
#if MECHADOG_WATCHDOG_FAULT_PROBE
  const char* const fault_probe = "true";
#else
  const char* const fault_probe = "false";
#endif
  char body[768];
  snprintf(body, sizeof(body),
           "{\"mac\":\"%s\",\"boot\":\"%s\",\"version\":\"%s\","
           "\"slot\":\"%s\",\"image_sha256\":\"%s\",\"healthy\":%s,"
           "\"confirmed\":%s,\"updating\":%s,\"actuators\":%s,\"service_mode\":%s,"
           "\"parked\":%s,\"slot_size\":%lu,\"loop_watchdog_armed\":%s,"
           "\"loop_watchdog_deadline_ms\":%u,\"watchdog_fault_probe\":%s}",
           g_mac, g_boot, MECHADOG_OTA_VERSION, running->label, g_image_sha,
           g_healthy ? "true" : "false", g_confirmed ? "true" : "false",
           g_updating ? "true" : "false", MECHADOG_ENABLE_ACTUATORS ? "true" : "false",
           mechadog::serviceModeParked() ? "true" : "false",
           mechadog::otaParkedForReboot() ? "true" : "false", static_cast<unsigned long>(kSlotSize),
           watchdog_armed, watchdog_deadline_ms, fault_probe);
  return reply(req, "200 OK", body);
}

esp_err_t confirmHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  // Confirming reboots a pending image; an actuator build must be parked or
  // safe-latched (pending-verify refuses RESET_SAFE, so latched = stationary).
  if (!mechadog::otaParkedForReboot())
    return reply(req, "409 Conflict", "{\"error\":\"not_parked\"}");
  if (!g_healthy || g_updating) return reply(req, "409 Conflict", "{\"error\":\"not_healthy\"}");
  g_confirm_requested = true;
  return reply(req, "202 Accepted", "{\"confirmation_requested\":true}");
}

esp_err_t updateHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  // Flash writes end in a reboot; an actuator build accepts them only while
  // SERVICE mode has parked the body. Actuator-OFF builds are always parked.
  if (!mechadog::serviceModeParked())
    return reply(req, "409 Conflict", "{\"error\":\"not_parked\"}");
  if (!g_confirmed || g_updating || !g_healthy)
    return reply(req, "409 Conflict", "{\"error\":\"not_ready\"}");
  char expected[65] = {};
  if (req->content_len < 256 || req->content_len > kSlotSize ||
      httpd_req_get_hdr_value_len(req, "X-Image-SHA256") != 64 ||
      httpd_req_get_hdr_value_str(req, "X-Image-SHA256", expected, sizeof(expected)) != ESP_OK)
    return reply(req, "400 Bad Request", "{\"error\":\"image_metadata\"}");
  if (!std::all_of(expected, expected + 64,
                   [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }))
    return reply(req, "400 Bad Request", "{\"error\":\"sha_format\"}");
  const auto* next = esp_ota_get_next_update_partition(nullptr);
  if (!next || next->size != kSlotSize || next == esp_ota_get_running_partition())
    return reply(req, "409 Conflict", "{\"error\":\"partition\"}");
  g_updating = true;
  g_healthy = false;
  // Flash operations may delay sensor acquisition; maintenance never promises
  // continuous sensor validity. Success AND interrupted writes reboot into a
  // checked app instead of silently accepting a stale/faulted sensor filter.
  esp_ota_handle_t handle = 0;
  esp_err_t err = esp_ota_begin(next, OTA_WITH_SEQUENTIAL_WRITES, &handle);
  bool opened = err == ESP_OK;
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts_ret(&sha, 0);
  uint8_t buffer[2048];
  size_t received = 0;
  const int64_t deadline = esp_timer_get_time() + kTransferDeadlineUs;
  while (err == ESP_OK && received < req->content_len) {
    if (esp_timer_get_time() > deadline) {
      err = ESP_ERR_TIMEOUT;
      break;
    }
    const size_t want = std::min(sizeof(buffer), req->content_len - received);
    const int count = httpd_req_recv(req, reinterpret_cast<char*>(buffer), want);
    if (count <= 0) {
      err = ESP_ERR_TIMEOUT;
      break;
    }
    err = esp_ota_write(handle, buffer, count);
    if (err == ESP_OK)
      err = mbedtls_sha256_update_ret(&sha, buffer, count) == 0 ? ESP_OK : ESP_FAIL;
    received += count;
  }
  uint8_t digest[32];
  char actual[65];
  mbedtls_sha256_finish_ret(&sha, digest);
  mbedtls_sha256_free(&sha);
  toHex(digest, sizeof(digest), actual);
  if (err == ESP_OK && !equalSecret(expected, actual, 64)) err = ESP_ERR_INVALID_CRC;
  if (opened) {
    if (err == ESP_OK)
      err = esp_ota_end(handle);
    else
      esp_ota_abort(handle);
  }
  if (err == ESP_OK) err = esp_ota_set_boot_partition(next);
  const esp_err_t sent =
      err == ESP_OK
          ? reply(req, "202 Accepted", "{\"staged\":true,\"rebooting\":true}")
          : reply(req, "400 Bad Request", "{\"staged\":false,\"rebooting_previous\":true}");
  g_reboot.schedule(millis(), 500);
  return sent;
}

void startServer(void*) {
  httpd_ssl_config_t config = HTTPD_SSL_CONFIG_DEFAULT();
  config.port_secure = 8443;
  config.httpd.core_id = 0;
  config.httpd.task_priority = 2;
  config.httpd.max_open_sockets = 2;
  config.httpd.recv_wait_timeout = 5;
  config.httpd.send_wait_timeout = 5;
  config.httpd.max_uri_handlers = 12;
  config.cacert_pem = reinterpret_cast<const uint8_t*>(kCertificate);
  config.cacert_len = sizeof(kCertificate);
  config.prvtkey_pem = reinterpret_cast<const uint8_t*>(kPrivateKey);
  config.prvtkey_len = sizeof(kPrivateKey);
  if (httpd_ssl_start(&g_server, &config) == ESP_OK) {
    const httpd_uri_t status = {"/status", HTTP_GET, statusHandler, nullptr};
    const httpd_uri_t confirm = {"/confirm", HTTP_POST, confirmHandler, nullptr};
    const httpd_uri_t update = {"/firmware", HTTP_POST, updateHandler, nullptr};
    const httpd_uri_t i2c = {"/i2c", HTTP_GET, i2cHandler, nullptr};
    const httpd_uri_t i2c_live = {"/i2c/live", HTTP_GET, i2cLiveHandler, nullptr};
    const httpd_uri_t i2c_read = {"/i2c/read", HTTP_GET, i2cReadHandler, nullptr};
    const httpd_uri_t i2c_write = {"/i2c/write", HTTP_POST, i2cWriteHandler, nullptr};
    const httpd_uri_t reboot = {"/reboot", HTTP_POST, rebootHandler, nullptr};
    g_started = httpd_register_uri_handler(g_server, &status) == ESP_OK &&
                httpd_register_uri_handler(g_server, &confirm) == ESP_OK &&
                httpd_register_uri_handler(g_server, &update) == ESP_OK &&
                httpd_register_uri_handler(g_server, &i2c) == ESP_OK &&
                httpd_register_uri_handler(g_server, &i2c_live) == ESP_OK &&
                httpd_register_uri_handler(g_server, &i2c_read) == ESP_OK &&
                httpd_register_uri_handler(g_server, &i2c_write) == ESP_OK &&
                httpd_register_uri_handler(g_server, &reboot) == ESP_OK;
  }
  Serial.printf("OTA HTTPS: started=%d version=%s port=8443\n", bool(g_started),
                MECHADOG_OTA_VERSION);
  vTaskDelete(nullptr);
}
}  // namespace
#endif

namespace mechadog {
bool beginStationaryOta() {
#if MECHADOG_ENABLE_OTA
  // 센서 태스크가 Wire 를 잡기 전에 끝낸다. setup() 에서 이 함수가
  // g_sensors.begin() 보다 먼저 호출되는 순서에 의존한다.
  scanI2cOnce();
  const auto* running = esp_ota_get_running_partition();
  const auto* next = esp_ota_get_next_update_partition(nullptr);
  if (!running || !next || running->size != kSlotSize || next->size != kSlotSize ||
      running->address == next->address)
    return false;
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  toHex(mac, sizeof(mac), g_mac);
  uint8_t boot[8];
  esp_fill_random(boot, sizeof(boot));
  toHex(boot, sizeof(boot), g_boot);
  uint8_t digest[32];
  if (esp_partition_get_sha256(running, digest) != ESP_OK) return false;
  toHex(digest, sizeof(digest), g_image_sha);
  esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
  g_pending =
      esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY;
  g_confirmed = !g_pending && state == ESP_OTA_IMG_VALID;
  if (g_pending) {
    esp_timer_create_args_t timer = {};
    timer.callback = rebootDeadline;
    timer.name = "ota-verify";
    if (esp_timer_create(&timer, &g_verify_timer) != ESP_OK ||
        esp_timer_start_once(g_verify_timer, kVerifyDeadlineUs) != ESP_OK)
      return false;
  }
  Serial.printf("OTA boot: version=%s slot=%s pending=%d\n", MECHADOG_OTA_VERSION, running->label,
                g_pending);
#endif
  return true;
}

void pollStationaryOta(bool wifi_connected, const SensorSnapshot& sample) {
#if MECHADOG_ENABLE_OTA
  if (g_reboot.due(millis())) esp_restart();
  if (wifi_connected && !g_start_requested.exchange(true)) {
    if (xTaskCreatePinnedToCore(startServer, "ota-start", 6144, nullptr, 2, nullptr, 0) != pdPASS)
      g_start_requested = false;
  }
  const uint32_t now = millis();
  const uint32_t age =
      std::max(sample.imu_age_ms, std::max(sample.dist_age_ms, sample.batt_age_ms));
  const bool valid = sample.all_valid() && age <= kSensorMaxAgeMs;
  const bool busy = sample.imu_error == SensorError::SnapshotBusy &&
                    sample.dist_error == SensorError::SnapshotBusy &&
                    sample.batt_error == SensorError::SnapshotBusy;
  g_healthy = g_health.observe(now, wifi_connected && g_started && !g_updating, valid, busy,
                               valid ? kSensorMaxAgeMs - age : 0);
#ifdef MECHADOG_OTA_TEST_REJECT_BOOT
  g_healthy = false;  // Dedicated rollback-test build, never release this image.
#endif
  if (g_confirm_requested.exchange(false) && g_healthy) {
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
      g_confirmed = true;
      if (g_verify_timer) esp_timer_stop(g_verify_timer);
      Serial.println("OTA confirmed: authenticated PC + healthy stationary runtime");
      // Confirming writes otadata and can pause cache/sensor acquisition.
      // Reboot the now-VALID image to restart filters after this flash write.
      if (g_pending) g_reboot.schedule(millis(), 500);
    }
  }
#else
  (void)wifi_connected;
  (void)sample;
#endif
}

bool otaPendingVerify() {
#if MECHADOG_ENABLE_OTA
  return g_pending && !g_confirmed;
#else
  return false;
#endif
}
}  // namespace mechadog
