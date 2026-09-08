// MechDog XIAO ESP32S3 Sense 비전 노드 (WBS 4.2.1, 4.2.2).
//
// 역할은 촬영과 MJPEG 송출뿐이다. 객체 검출은 Host PC가 맡는다(DR-3).
// 포트 80은 상태/프로파일 제어, 포트 81은 장시간 점유되는 MJPEG 스트림으로 분리한다.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_http_server.h>
#include <esp_timer.h>

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#define MECHDOG_WIFI_SSID "YOUR_WIFI_SSID"
#define MECHDOG_WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#endif

namespace {

// Seeed Studio XIAO ESP32S3 Sense 공식 카메라 핀 배열.
constexpr int kPinPwdn = -1;
constexpr int kPinReset = -1;
constexpr int kPinXclk = 10;
constexpr int kPinSiod = 40;
constexpr int kPinSioc = 39;
constexpr int kPinD7 = 48;
constexpr int kPinD6 = 11;
constexpr int kPinD5 = 12;
constexpr int kPinD4 = 14;
constexpr int kPinD3 = 16;
constexpr int kPinD2 = 18;
constexpr int kPinD1 = 17;
constexpr int kPinD0 = 15;
constexpr int kPinVsync = 38;
constexpr int kPinHref = 47;
constexpr int kPinPclk = 13;

constexpr uint16_t kControlPort = 80;
constexpr uint16_t kStreamPort = 81;
constexpr uint32_t kWifiTimeoutMs = 20000;
constexpr uint32_t kReconnectIntervalMs = 5000;
constexpr uint32_t kFpsReportIntervalMs = 5000;

// 프레임률 상한의 기본값. 호스트가 기동 시 config.yaml 값으로 덮어쓴다.
// ⚠️ 상한의 목적은 낮추는 것이 아니라 **예측 가능하게 만드는 것**이다. 상한이
// 없으면 JPEG 크기가 화면 내용에 따라 변하고 발열에 따라 fps 가 흘러서, 명령
// 타임아웃 대비 최악 부하를 계산할 수 없다.
constexpr uint32_t kDefaultFpsLimit = 25;
constexpr uint32_t kMinFpsLimit = 1;
constexpr uint32_t kMaxFpsLimit = 60;

constexpr char kStreamContentType[] = "multipart/x-mixed-replace;boundary=mechdog-frame-boundary";
constexpr char kStreamBoundary[] = "\r\n--mechdog-frame-boundary\r\n";

httpd_handle_t g_control_server = nullptr;
httpd_handle_t g_stream_server = nullptr;
framesize_t g_frame_size = FRAMESIZE_VGA;
const char* g_profile_name = "VGA";
bool g_camera_ready = false;
uint32_t g_last_reconnect_ms = 0;
uint32_t g_fps_limit = kDefaultFpsLimit;

bool hasCredentials() {
  return strcmp(MECHDOG_WIFI_SSID, "YOUR_WIFI_SSID") != 0 && strlen(MECHDOG_WIFI_SSID) > 0;
}

const char* sensorName(uint16_t pid) {
  switch (pid) {
    case OV2640_PID:
      return "OV2640";
    case OV3660_PID:
      return "OV3660";
    default:
      return "UNKNOWN";
  }
}

bool initializeCamera() {
  if (!psramFound()) {
    Serial.println("ERROR camera_init: PSRAM을 찾지 못했습니다");
    return false;
  }

  camera_config_t config{};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = kPinD0;
  config.pin_d1 = kPinD1;
  config.pin_d2 = kPinD2;
  config.pin_d3 = kPinD3;
  config.pin_d4 = kPinD4;
  config.pin_d5 = kPinD5;
  config.pin_d6 = kPinD6;
  config.pin_d7 = kPinD7;
  config.pin_xclk = kPinXclk;
  config.pin_pclk = kPinPclk;
  config.pin_vsync = kPinVsync;
  config.pin_href = kPinHref;
  config.pin_sccb_sda = kPinSiod;
  config.pin_sccb_scl = kPinSioc;
  config.pin_pwdn = kPinPwdn;
  config.pin_reset = kPinReset;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = g_frame_size;
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  const esp_err_t error = esp_camera_init(&config);
  if (error != ESP_OK) {
    Serial.printf("ERROR camera_init: 0x%x\n", error);
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("ERROR camera_init: sensor handle 없음");
    return false;
  }

  // 현행 OV3660과 구형 OV2640을 모두 같은 펌웨어에서 식별한다.
  // 모듈 장착 방향 때문에 OV3660 기본 영상은 뒤집혀 보인다.
  if (sensor->id.PID == OV3660_PID) {
    sensor->set_vflip(sensor, 1);
  }
  sensor->set_framesize(sensor, g_frame_size);

  Serial.printf("CAMERA_READY sensor=%s psram_free=%u profile=%s\n", sensorName(sensor->id.PID),
                static_cast<unsigned>(ESP.getFreePsram()), g_profile_name);
  return true;
}

bool connectWifi() {
  if (!hasCredentials()) {
    Serial.println("ERROR wifi_config: wifi_secrets.example.h를 wifi_secrets.h로 복사하세요");
    return false;
  }

  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);

  const uint32_t started_ms = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started_ms < kWifiTimeoutMs) {
    delay(250);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("WARN wifi_connect: status=%d\n", static_cast<int>(WiFi.status()));
    return false;
  }

  Serial.printf("WIFI_READY ip=%s rssi=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

esp_err_t sendJson(httpd_req_t* request, const char* body) {
  httpd_resp_set_type(request, "application/json; charset=utf-8");
  httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
  return httpd_resp_sendstr(request, body);
}

esp_err_t statusHandler(httpd_req_t* request) {
  sensor_t* sensor = esp_camera_sensor_get();
  const uint16_t pid = sensor == nullptr ? 0 : sensor->id.PID;
  char body[288];
  snprintf(body, sizeof(body),
           "{\"ok\":true,\"sensor\":\"%s\",\"profile\":\"%s\",\"fps_limit\":%u,"
           "\"psram_free\":%u,\"rssi\":%d,\"stream\":\"http://%s:%u/stream\"}",
           sensorName(pid), g_profile_name, static_cast<unsigned>(g_fps_limit),
           static_cast<unsigned>(ESP.getFreePsram()), WiFi.RSSI(),
           WiFi.localIP().toString().c_str(), kStreamPort);
  return sendJson(request, body);
}

esp_err_t profileHandler(httpd_req_t* request) {
  char query[64] = {};
  char name[12] = {};
  char fps_text[8] = {};
  const size_t query_length = httpd_req_get_url_query_len(request);
  if (query_length == 0 || query_length >= sizeof(query) ||
      httpd_req_get_url_query_str(request, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "name", name, sizeof(name)) != ESP_OK) {
    httpd_resp_set_status(request, "400 Bad Request");
    return sendJson(request, "{\"ok\":false,\"error\":\"name must be VGA or QVGA\"}");
  }

  framesize_t requested_size;
  const char* requested_name;
  if (strcasecmp(name, "VGA") == 0) {
    requested_size = FRAMESIZE_VGA;
    requested_name = "VGA";
  } else if (strcasecmp(name, "QVGA") == 0) {
    requested_size = FRAMESIZE_QVGA;
    requested_name = "QVGA";
  } else {
    httpd_resp_set_status(request, "400 Bad Request");
    return sendJson(request, "{\"ok\":false,\"error\":\"name must be VGA or QVGA\"}");
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr || sensor->set_framesize(sensor, requested_size) != 0) {
    httpd_resp_set_status(request, "500 Internal Server Error");
    return sendJson(request, "{\"ok\":false,\"error\":\"camera rejected profile\"}");
  }
  g_frame_size = requested_size;
  g_profile_name = requested_name;

  // fps 는 선택 인자다. 없으면 현재 상한을 유지한다 — 해상도만 바꾸는 호출을
  // 막지 않기 위해서다.
  if (httpd_query_key_value(query, "fps", fps_text, sizeof(fps_text)) == ESP_OK) {
    const long requested_fps = strtol(fps_text, nullptr, 10);
    if (requested_fps < static_cast<long>(kMinFpsLimit) ||
        requested_fps > static_cast<long>(kMaxFpsLimit)) {
      httpd_resp_set_status(request, "400 Bad Request");
      return sendJson(request, "{\"ok\":false,\"error\":\"fps out of range\"}");
    }
    g_fps_limit = static_cast<uint32_t>(requested_fps);
  }

  char body[112];
  snprintf(body, sizeof(body), "{\"ok\":true,\"profile\":\"%s\",\"fps_limit\":%u}", g_profile_name,
           static_cast<unsigned>(g_fps_limit));
  Serial.printf("PROFILE_CHANGED profile=%s fps_limit=%u\n", g_profile_name,
                static_cast<unsigned>(g_fps_limit));
  return sendJson(request, body);
}

esp_err_t streamHandler(httpd_req_t* request) {
  esp_err_t result = httpd_resp_set_type(request, kStreamContentType);
  if (result != ESP_OK) {
    return result;
  }
  httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");

  uint32_t frame_count = 0;
  uint32_t skipped = 0;
  uint32_t sent_bytes = 0;
  int64_t report_started_us = esp_timer_get_time();
  int64_t next_due_us = report_started_us;
  while (result == ESP_OK) {
    camera_fb_t* frame = esp_camera_fb_get();
    if (frame == nullptr) {
      Serial.println("WARN stream: frame capture failed");
      result = ESP_FAIL;
      break;
    }

    // ── 프레임률 상한 ──
    // ⚠️ **잡은 뒤에 버린다.** 먼저 기다렸다가 잡으면 그 사이 시간만큼 낡은
    // 프레임을 보내게 된다. 상한의 목적은 신선한 프레임을 덜 보내는 것이고
    // 낡은 프레임을 보내는 것이 아니다.
    const int64_t now_us = esp_timer_get_time();
    if (now_us < next_due_us) {
      esp_camera_fb_return(frame);
      ++skipped;
      continue;
    }
    const int64_t period_us = 1000000 / static_cast<int64_t>(g_fps_limit);
    next_due_us += period_us;
    if (next_due_us <= now_us) {
      // 크게 밀렸으면 과거를 따라잡지 않고 지금 기준으로 재동기한다. 몰아
      // 보내면 순간 폭주가 되고 명령 패킷의 순서를 더 밀어낸다.
      next_due_us = now_us + period_us;
    }

    char header[96];
    const int header_length =
        snprintf(header, sizeof(header), "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                 static_cast<unsigned>(frame->len));
    if (header_length <= 0 || static_cast<size_t>(header_length) >= sizeof(header)) {
      esp_camera_fb_return(frame);
      result = ESP_FAIL;
      break;
    }

    result = httpd_resp_send_chunk(request, kStreamBoundary, strlen(kStreamBoundary));
    if (result == ESP_OK) {
      result = httpd_resp_send_chunk(request, header, static_cast<size_t>(header_length));
    }
    if (result == ESP_OK) {
      result =
          httpd_resp_send_chunk(request, reinterpret_cast<const char*>(frame->buf), frame->len);
    }
    sent_bytes += static_cast<uint32_t>(frame->len);
    esp_camera_fb_return(frame);

    ++frame_count;
    const int64_t report_now_us = esp_timer_get_time();
    if (report_now_us - report_started_us >= static_cast<int64_t>(kFpsReportIntervalMs) * 1000) {
      const float elapsed_s = static_cast<float>(report_now_us - report_started_us) / 1000000.0F;
      const float fps = static_cast<float>(frame_count) / elapsed_s;
      // 평균 프레임 크기를 함께 남긴다 — fps 만으로는 대역폭을 알 수 없고,
      // JPEG 크기는 화면 내용에 따라 변한다. Wi-Fi 경합을 따질 때 필요한 값이다.
      const uint32_t bytes_avg = frame_count == 0 ? 0 : sent_bytes / frame_count;
      Serial.printf("STREAM_STATS profile=%s fps=%.1f limit=%u bytes_avg=%u skipped=%u kbps=%.0f\n",
                    g_profile_name, fps, static_cast<unsigned>(g_fps_limit),
                    static_cast<unsigned>(bytes_avg), static_cast<unsigned>(skipped),
                    static_cast<double>(sent_bytes) * 8.0 / 1000.0 / elapsed_s);
      frame_count = 0;
      skipped = 0;
      sent_bytes = 0;
      report_started_us = report_now_us;
    }
  }

  Serial.printf("STREAM_CLOSED result=0x%x\n", result);
  return result;
}

bool startServers() {
  httpd_config_t control_config = HTTPD_DEFAULT_CONFIG();
  control_config.server_port = kControlPort;
  control_config.ctrl_port = 32768;

  httpd_uri_t status_uri{};
  status_uri.uri = "/";
  status_uri.method = HTTP_GET;
  status_uri.handler = statusHandler;

  httpd_uri_t profile_uri{};
  profile_uri.uri = "/profile";
  profile_uri.method = HTTP_GET;
  profile_uri.handler = profileHandler;
  if (httpd_start(&g_control_server, &control_config) != ESP_OK ||
      httpd_register_uri_handler(g_control_server, &status_uri) != ESP_OK ||
      httpd_register_uri_handler(g_control_server, &profile_uri) != ESP_OK) {
    Serial.println("ERROR http_server: control server start failed");
    return false;
  }

  httpd_config_t stream_config = HTTPD_DEFAULT_CONFIG();
  stream_config.server_port = kStreamPort;
  stream_config.ctrl_port = 32769;
  httpd_uri_t stream_uri{};
  stream_uri.uri = "/stream";
  stream_uri.method = HTTP_GET;
  stream_uri.handler = streamHandler;
  if (httpd_start(&g_stream_server, &stream_config) != ESP_OK ||
      httpd_register_uri_handler(g_stream_server, &stream_uri) != ESP_OK) {
    Serial.println("ERROR http_server: stream server start failed");
    return false;
  }

  Serial.printf("HTTP_READY status=http://%s/ stream=http://%s:%u/stream\n",
                WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str(), kStreamPort);
  return true;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("BOOT mechdog-xiao-vision");

  g_camera_ready = initializeCamera();
  if (!g_camera_ready) {
    return;
  }
  if (connectWifi()) {
    startServers();
  }
}

void loop() {
  if (!g_camera_ready) {
    delay(1000);
    return;
  }

  if (WiFi.status() != WL_CONNECTED && millis() - g_last_reconnect_ms >= kReconnectIntervalMs) {
    g_last_reconnect_ms = millis();
    if (connectWifi() && g_stream_server == nullptr) {
      startServers();
    }
  }
  delay(100);
}
