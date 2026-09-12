#include "stationary_ota.h"

#include "ota_health.h"
#include "reboot_deadline.h"
#include "sensor_hal.h"

#if MECHADOG_ENABLE_OTA
#include <Arduino.h>
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

#if MECHADOG_ENABLE_ACTUATORS
#error "Stationary OTA requires actuators OFF; moving maintenance is not integrated"
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

esp_err_t statusHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  const auto* running = esp_ota_get_running_partition();
  char body[512];
  snprintf(body, sizeof(body),
           "{\"mac\":\"%s\",\"boot\":\"%s\",\"version\":\"%s\","
           "\"slot\":\"%s\",\"image_sha256\":\"%s\",\"healthy\":%s,"
           "\"confirmed\":%s,\"updating\":%s,\"actuators\":false,\"slot_size\":%lu}",
           g_mac, g_boot, MECHADOG_OTA_VERSION, running->label, g_image_sha,
           g_healthy ? "true" : "false", g_confirmed ? "true" : "false",
           g_updating ? "true" : "false", static_cast<unsigned long>(kSlotSize));
  return reply(req, "200 OK", body);
}

esp_err_t confirmHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
  if (!g_healthy || g_updating) return reply(req, "409 Conflict", "{\"error\":\"not_healthy\"}");
  g_confirm_requested = true;
  return reply(req, "202 Accepted", "{\"confirmation_requested\":true}");
}

esp_err_t updateHandler(httpd_req_t* req) {
  if (!authorized(req)) return reply(req, "401 Unauthorized", "{\"error\":\"auth\"}");
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
  config.cacert_pem = reinterpret_cast<const uint8_t*>(kCertificate);
  config.cacert_len = sizeof(kCertificate);
  config.prvtkey_pem = reinterpret_cast<const uint8_t*>(kPrivateKey);
  config.prvtkey_len = sizeof(kPrivateKey);
  if (httpd_ssl_start(&g_server, &config) == ESP_OK) {
    const httpd_uri_t status = {"/status", HTTP_GET, statusHandler, nullptr};
    const httpd_uri_t confirm = {"/confirm", HTTP_POST, confirmHandler, nullptr};
    const httpd_uri_t update = {"/firmware", HTTP_POST, updateHandler, nullptr};
    g_started = httpd_register_uri_handler(g_server, &status) == ESP_OK &&
                httpd_register_uri_handler(g_server, &confirm) == ESP_OK &&
                httpd_register_uri_handler(g_server, &update) == ESP_OK;
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
}  // namespace mechadog
