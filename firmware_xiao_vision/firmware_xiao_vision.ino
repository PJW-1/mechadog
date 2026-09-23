// MechDog XIAO ESP32S3 Sense 비전 노드 (WBS 4.2.1, 4.2.2).
//
// 역할은 촬영과 MJPEG 송출뿐이다. 객체 검출은 Host PC가 맡는다(DR-3).
// 포트 80은 상태/프로파일 제어, 포트 81은 장시간 점유되는 MJPEG 스트림으로 분리한다.
// 포트 82는 확장보드 PDM 마이크의 16 kHz PCM16 스트림이다(음성 인식은 Host PC).

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <esp_camera.h>
#include <esp_http_server.h>
#include <esp_timer.h>
#include <esp_wifi.h>
#include <lwip/sockets.h>

#ifndef MECHDOG_STREAM_TCP_NODELAY
#define MECHDOG_STREAM_TCP_NODELAY 1
#endif

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
constexpr uint16_t kAudioPort = 82;
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
httpd_handle_t g_audio_server = nullptr;
framesize_t g_frame_size = FRAMESIZE_VGA;
const char* g_profile_name = "VGA";
bool g_camera_ready = false;
bool g_wifi_initialized = false;
bool g_wifi_reported = false;
uint32_t g_last_reconnect_ms = 0;
uint32_t g_fps_limit = kDefaultFpsLimit;
// 모듈을 광축 기준으로 180° 돌려 다는 장착(브래킷 위치 때문에 뒤집어야 하는
// 경우)을 런타임에 바로잡는다. 물리 회전은 센서 레지스터의 vflip+hmirror
// 합성과 같으므로 재플래시 없이 /orient?rot=180 한 번으로 교정된다.
bool g_mount_rotated = false;

// ── 마이크 ──
// 확장보드 PDM 마이크: CLK=GPIO42, DATA=GPIO41. 카메라 핀과 겹치지 않고, S3 의 카메라는
// I2S 가 아니라 LCD_CAM 주변장치를 쓰므로 I2S0 도 비어 있다.
constexpr int kPinPdmClk = 42;
constexpr int kPinPdmData = 41;
constexpr int kAudioRate = 16000;
constexpr size_t kAudioChunkSamples = 512;  // 32 ms
// DMA 버퍼 8 x 512 샘플 = 256 ms. 전송이 이보다 오래 막히면 샘플이 버려지고
// I2S_EVENT_RX_Q_OVF 로 센다 — 끊김을 숨기지 않고 AUDIO_STATS 에 남긴다.
constexpr int kAudioDmaCount = 8;
constexpr int kAudioDefaultGain = 2;  // 왼쪽 시프트. mic_probe 로 인식률을 확인한 값
bool g_mic_ready = false;
QueueHandle_t g_i2s_events = nullptr;
int16_t g_audio_chunk[kAudioChunkSamples];

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

// 장착 방향 보정을 센서 레지스터에 적용한다. 물리 180° 회전은 영상의
// vflip+hmirror 와 같으므로, 뒤집어 단 경우 기본 보정에서 두 플래그를
// 모두 반전하면 된다 — OV3660 은 (1,0) → (0,1) 이 된다.
void applyOrientation(sensor_t* sensor) {
  const bool ov3660 = (sensor->id.PID == OV3660_PID);
  const int base_vflip = ov3660 ? 1 : 0;
  sensor->set_vflip(sensor, base_vflip ^ (g_mount_rotated ? 1 : 0));
  sensor->set_hmirror(sensor, g_mount_rotated ? 1 : 0);
}

// OV3660 은 XCLK 가 돌기 시작한 뒤에야 SCCB 에 제대로 답한다. 그런데 드라이버는
// 클럭을 켜자마자 ID 레지스터를 읽으므로, 차가운 상태에서는 쓰레기 값을 읽는다.
//
// ⚠️ **그 실패가 «카메라가 없다» 처럼 보인다.** 2026-09-18 실측에서 코어 2.0.17 은
// `0x105 NOT_FOUND`, 3.3.11 은 `0x106 NOT_SUPPORTED` 로 끝났고, 둘 다 배선 불량과
// 구분되지 않아 멀쩡한 하드웨어를 의심하게 만들었다. 같은 보드에서 XCLK 를 먼저
// 200ms 돌린 뒤에는 `PID=0x3660` 으로 정상 초기화되고 VGA 프레임까지 나왔다.
//
// 그래서 **드라이버를 부르기 전에 클럭을 미리 돌려 센서를 깨운다.** 비용은 부팅
// 250ms 뿐이고, 없으면 카메라가 아예 살아나지 않는다.
void warmUpSensorClock() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  if (!ledcAttach(kPinXclk, 20000000, 1)) {
    Serial.println("WARN camera_init: XCLK 워밍업 실패 - 그대로 진행한다");
    return;
  }
  ledcWrite(kPinXclk, 1);  // 1비트 해상도에서 50% 듀티
#else
  ledcSetup(1, 20000000, 1);
  ledcAttachPin(kPinXclk, 1);
  ledcWrite(1, 1);
#endif
  delay(50);

  // SCCB 를 한 번 열어 센서에게 말을 걸어 둔다.
  //
  // ⚠️ **클럭만 돌려서는 부족했다.** 2026-09-18 실측에서 클럭만 200ms 돌린 뒤의
  // `esp_camera_init` 은 세 번 모두 `0x106 NOT_SUPPORTED` 였고, 같은 보드에서
  // `Wire` 로 ID 레지스터를 한 번 읽어 본 뒤에는 통과했다. `Wire.begin` 이 SDA·SCL 에
  // 내부 풀업을 걸어 주는데 새 `sccb-ng` 드라이버는 그것을 하지 않는 것으로 보인다.
  //
  // ⚠️ **읽은 값으로 분기하지 않는다.** 여기서 하는 일은 버스를 깨우는 것뿐이고,
  // 센서 판정은 드라이버에 맡긴다 — 같은 일을 두 곳에서 하면 언젠가 어긋난다.
  Wire.begin(kPinSiod, kPinSioc, 100000);
  delay(50);
  uint16_t chip_id = 0;
  Wire.beginTransmission(0x3C);
  Wire.write(0x30);
  Wire.write(0x0A);
  if (Wire.endTransmission(false) == 0 && Wire.requestFrom(0x3C, 1) == 1) {
    chip_id = static_cast<uint16_t>(Wire.read()) << 8;
    Wire.beginTransmission(0x3C);
    Wire.write(0x30);
    Wire.write(0x0B);
    if (Wire.endTransmission(false) == 0 && Wire.requestFrom(0x3C, 1) == 1) {
      chip_id |= Wire.read();
    }
  }
  Serial.printf("SENSOR_PROBE chip_id=0x%04X\n", chip_id);
  Wire.end();

#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcDetach(kPinXclk);
#else
  ledcDetachPin(kPinXclk);
#endif
  delay(200);
}

bool initializeCamera() {
  if (!psramFound()) {
    Serial.println("ERROR camera_init: PSRAM을 찾지 못했습니다");
    return false;
  }

  warmUpSensorClock();

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

  // ⚠️ **초기화가 간헐적으로 실패한다.** 같은 보드·같은 전원에서 한 번은
  // `CAMERA_READY` 가 나왔고, 다음 전원 인가에서는 `sccb-ng: W [6702]=fd fail` 로
  // 죽었다. 센서가 깨어나는 시점이 매번 같지 않다는 뜻이다.
  //
  // 원인을 한 줄로 확정하지 못했으므로 **실패를 견디게 만든다** — 부분 자원을
  // 되돌리고 클럭을 다시 깨운 뒤 다시 시도한다. 비용은 실패했을 때의 몇백 ms 뿐이고,
  // 없으면 카메라가 죽은 채로 로봇이 순찰을 돈다.
  esp_err_t error = ESP_FAIL;
  for (int attempt = 1; attempt <= 3; ++attempt) {
    error = esp_camera_init(&config);
    if (error == ESP_OK) {
      if (attempt > 1) {
        Serial.printf("WARN camera_init: %d회째에 성공\n", attempt);
      }
      break;
    }
    Serial.printf("WARN camera_init attempt=%d error=0x%x\n", attempt, error);
    esp_camera_deinit();
    delay(300);
    warmUpSensorClock();
  }
  if (error != ESP_OK) {
    Serial.printf("ERROR camera_init: 0x%x\n", error);
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    Serial.println("ERROR camera_init: sensor handle 없음");
    return false;
  }

  applyOrientation(sensor);
  sensor->set_framesize(sensor, g_frame_size);

  Serial.printf("CAMERA_READY sensor=%s psram_free=%u profile=%s\n", sensorName(sensor->id.PID),
                static_cast<unsigned>(ESP.getFreePsram()), g_profile_name);
  return true;
}

bool initializeMic() {
  i2s_config_t config{};
  config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
  config.sample_rate = kAudioRate;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL2;
  config.dma_buf_count = kAudioDmaCount;
  config.dma_buf_len = kAudioChunkSamples;
  i2s_pin_config_t pins{};
  pins.bck_io_num = I2S_PIN_NO_CHANGE;
  pins.ws_io_num = kPinPdmClk;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = kPinPdmData;
  // 채널 설정은 mic_probe 가 검증한 Arduino I2S 라이브러리의 PDM_MONO_MODE 와 같게 맞춘다.
  if (i2s_driver_install(I2S_NUM_0, &config, 8, &g_i2s_events) != ESP_OK ||
      i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK ||
      i2s_set_clk(I2S_NUM_0, kAudioRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO) != ESP_OK) {
    Serial.println("ERROR mic_init: i2s driver");
    return false;
  }
  // 받는 쪽이 없을 때는 멈춰 둔다. 계속 돌리면 DMA 가 넘치는 이벤트만 쌓인다.
  i2s_stop(I2S_NUM_0);
  Serial.printf("MIC_READY rate=%d dma_ms=%d\n", kAudioRate,
                static_cast<int>(kAudioDmaCount * kAudioChunkSamples * 1000 / kAudioRate));
  return true;
}

bool connectWifi() {
  if (!hasCredentials()) {
    Serial.println("ERROR wifi_config: wifi_secrets.example.h를 wifi_secrets.h로 복사하세요");
    return false;
  }

  if (!g_wifi_initialized) {
    if (!WiFi.mode(WIFI_STA)) {
      Serial.println("WARN wifi_init: station mode failed");
      return false;
    }
    WiFi.persistent(false);
    WiFi.setAutoReconnect(true);
    WiFi.setSleep(false);
    if (WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD) == WL_CONNECT_FAILED) {
      Serial.println("WARN wifi_init: station configuration failed");
      return false;
    }
    g_wifi_initialized = true;
    g_last_reconnect_ms = millis();
  }
  // Association and DHCP progress asynchronously. Do not tear them down with
  // another credential-bearing begin() just because address acquisition is slow.
  return WiFi.status() == WL_CONNECTED;
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
           "\"rot\":%u,\"psram_free\":%u,\"rssi\":%d,\"stream\":\"http://%s:%u/stream\"}",
           sensorName(pid), g_profile_name, static_cast<unsigned>(g_fps_limit),
           g_mount_rotated ? 180u : 0u, static_cast<unsigned>(ESP.getFreePsram()), WiFi.RSSI(),
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

esp_err_t orientHandler(httpd_req_t* request) {
  char query[24] = {};
  char rot_text[8] = {};
  const size_t query_length = httpd_req_get_url_query_len(request);
  if (query_length == 0 || query_length >= sizeof(query) ||
      httpd_req_get_url_query_str(request, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "rot", rot_text, sizeof(rot_text)) != ESP_OK) {
    httpd_resp_set_status(request, "400 Bad Request");
    return sendJson(request, "{\"ok\":false,\"error\":\"rot must be 0 or 180\"}");
  }
  const long rot = strtol(rot_text, nullptr, 10);
  if (rot != 0 && rot != 180) {
    httpd_resp_set_status(request, "400 Bad Request");
    return sendJson(request, "{\"ok\":false,\"error\":\"rot must be 0 or 180\"}");
  }
  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    httpd_resp_set_status(request, "500 Internal Server Error");
    return sendJson(request, "{\"ok\":false,\"error\":\"camera sensor unavailable\"}");
  }
  g_mount_rotated = (rot == 180);
  applyOrientation(sensor);

  char body[96];
  snprintf(body, sizeof(body), "{\"ok\":true,\"rot\":%ld}", rot);
  Serial.printf("ORIENT_CHANGED rot=%ld\n", rot);
  return sendJson(request, body);
}

esp_err_t streamHandler(httpd_req_t* request) {
  const int socket_fd = httpd_req_to_sockfd(request);
  int no_delay = -1;
  socklen_t option_length = sizeof(no_delay);
  if (socket_fd < 0 ||
      getsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, &no_delay, &option_length) != 0) {
    Serial.println("WARN stream_socket: cannot read TCP_NODELAY");
    return ESP_FAIL;
  }
  Serial.printf("STREAM_SOCKET nodelay_before=%d\n", no_delay);
#if MECHDOG_STREAM_TCP_NODELAY
  no_delay = 1;
  if (setsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay)) != 0) {
    Serial.println("WARN stream_socket: cannot set TCP_NODELAY");
    return ESP_FAIL;
  }
#endif
  option_length = sizeof(no_delay);
  if (getsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, &no_delay, &option_length) != 0) {
    return ESP_FAIL;
  }
#if MECHDOG_STREAM_TCP_NODELAY
  if (no_delay != 1) {
    return ESP_FAIL;
  }
#endif
  Serial.printf("STREAM_SOCKET nodelay_actual=%d\n", no_delay);
  esp_err_t result = httpd_resp_set_type(request, kStreamContentType);
  if (result != ESP_OK) {
    return result;
  }
  httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");

  uint32_t frame_count = 0;
  uint32_t skipped = 0;
  uint32_t sent_bytes = 0;
  int64_t capture_total_us = 0;
  int64_t capture_max_us = 0;
  int64_t send_total_us = 0;
  int64_t send_max_us = 0;
  int64_t age_total_us = 0;
  int64_t age_max_us = 0;
  int64_t report_started_us = esp_timer_get_time();
  int64_t next_due_us = report_started_us;
  while (result == ESP_OK) {
    const int64_t capture_started_us = esp_timer_get_time();
    camera_fb_t* frame = esp_camera_fb_get();
    const int64_t captured_us = esp_timer_get_time();
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
      // 프레임 큐가 연달아 준비돼 있어도 HTTP/Wi-Fi 태스크가 실행될 틈을 준다.
      vTaskDelay(1);
      continue;
    }
    const int64_t period_us = 1000000 / static_cast<int64_t>(g_fps_limit);
    next_due_us += period_us;
    if (next_due_us <= now_us) {
      // 크게 밀렸으면 과거를 따라잡지 않고 지금 기준으로 재동기한다. 몰아
      // 보내면 순간 폭주가 되고 명령 패킷의 순서를 더 밀어낸다.
      next_due_us = now_us + period_us;
    }

    // Boundary and JPEG metadata share one HTTP chunk; the JPEG buffer stays in place.
    char header[160];
    const int header_length =
        snprintf(header, sizeof(header), "%sContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                 kStreamBoundary, static_cast<unsigned>(frame->len));
    if (header_length <= 0 || static_cast<size_t>(header_length) >= sizeof(header)) {
      esp_camera_fb_return(frame);
      result = ESP_FAIL;
      break;
    }

    const int64_t send_started_us = esp_timer_get_time();
    result = httpd_resp_send_chunk(request, header, static_cast<size_t>(header_length));
    if (result == ESP_OK) {
      result =
          httpd_resp_send_chunk(request, reinterpret_cast<const char*>(frame->buf), frame->len);
    }
    const int64_t sent_us = esp_timer_get_time();
    const int64_t capture_us = captured_us - capture_started_us;
    const int64_t send_us = sent_us - send_started_us;
    // Camera driver timestamp is on this ESP's monotonic clock. This is local
    // frame age at send start, not PC arrival latency or optical E2E latency.
    const int64_t timestamp_us =
        static_cast<int64_t>(frame->timestamp.tv_sec) * 1000000 + frame->timestamp.tv_usec;
    const int64_t age_us = send_started_us > timestamp_us ? send_started_us - timestamp_us : 0;
    if (result == ESP_OK) {
      capture_total_us += capture_us;
      capture_max_us = max(capture_max_us, capture_us);
      send_total_us += send_us;
      send_max_us = max(send_max_us, send_us);
      age_total_us += age_us;
      age_max_us = max(age_max_us, age_us);
      sent_bytes += static_cast<uint32_t>(frame->len);
      ++frame_count;
    } else {
      Serial.printf("STREAM_SEND_FAILED result=0x%x elapsed_us=%lld\n", result, send_us);
    }
    esp_camera_fb_return(frame);

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
      if (frame_count > 0) {
        Serial.printf(
            "STREAM_TIMING capture_avg_us=%lld capture_max_us=%lld send_avg_us=%lld "
            "send_max_us=%lld age_avg_us=%lld age_max_us=%lld rssi=%d heap=%u\n",
            capture_total_us / frame_count, capture_max_us, send_total_us / frame_count,
            send_max_us, age_total_us / frame_count, age_max_us, WiFi.RSSI(),
            static_cast<unsigned>(ESP.getFreeHeap()));
      }
      frame_count = 0;
      skipped = 0;
      sent_bytes = 0;
      capture_total_us = capture_max_us = 0;
      send_total_us = send_max_us = 0;
      age_total_us = age_max_us = 0;
      report_started_us = report_now_us;
    }
  }

  Serial.printf("STREAM_CLOSED result=0x%x\n", result);
  return result;
}

// 클라이언트 하나에게 16 kHz 모노 PCM16LE 를 끊지 않고 흘려보낸다.
// /audio?gain=0..4 (기본 2). DC 는 1차 고역 통과로 지운다 — 덩어리마다 평균을 빼면
// 덩어리 경계마다 계단이 생긴다.
esp_err_t audioHandler(httpd_req_t* request) {
  if (!g_mic_ready) {
    httpd_resp_set_status(request, "503 Service Unavailable");
    return sendJson(request, "{\"ok\":false,\"error\":\"mic unavailable\"}");
  }
  int gain = kAudioDefaultGain;
  char query[24] = {};
  char gain_text[4] = {};
  if (httpd_req_get_url_query_str(request, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "gain", gain_text, sizeof(gain_text)) == ESP_OK) {
    gain = constrain(static_cast<int>(strtol(gain_text, nullptr, 10)), 0, 4);
  }
  httpd_resp_set_type(request, "audio/L16;rate=16000;channels=1");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");

  xQueueReset(g_i2s_events);
  i2s_zero_dma_buffer(I2S_NUM_0);
  i2s_start(I2S_NUM_0);
  Serial.printf("AUDIO_OPEN gain=%d\n", gain);

  size_t got = 0;
  // 시작 직후 ~100 ms 는 PDM 필터가 안정되는 구간이다.
  for (int i = 0; i < 3; ++i) {
    i2s_read(I2S_NUM_0, g_audio_chunk, sizeof(g_audio_chunk), &got, portMAX_DELAY);
  }

  float x_prev = 0;
  float y_prev = 0;
  uint32_t sent_bytes = 0;
  uint32_t overflows = 0;
  int64_t send_max_us = 0;
  double square_sum = 0;
  uint32_t sample_count = 0;
  int64_t report_started_us = esp_timer_get_time();
  esp_err_t result = ESP_OK;
  while (result == ESP_OK) {
    if (i2s_read(I2S_NUM_0, g_audio_chunk, sizeof(g_audio_chunk), &got, portMAX_DELAY) != ESP_OK) {
      result = ESP_FAIL;
      break;
    }
    const size_t samples = got / sizeof(int16_t);
    for (size_t i = 0; i < samples; ++i) {
      const float x = g_audio_chunk[i];
      const float y = x - x_prev + 0.995F * y_prev;
      x_prev = x;
      y_prev = y;
      const int32_t v = constrain(static_cast<int32_t>(y) * (1 << gain), -32768, 32767);
      g_audio_chunk[i] = static_cast<int16_t>(v);
      square_sum += static_cast<double>(v) * v;
    }
    sample_count += samples;

    const int64_t send_started_us = esp_timer_get_time();
    result = httpd_resp_send_chunk(request, reinterpret_cast<const char*>(g_audio_chunk), got);
    send_max_us = max(send_max_us, esp_timer_get_time() - send_started_us);
    if (result == ESP_OK) {
      sent_bytes += got;
    }

    i2s_event_t event;
    while (xQueueReceive(g_i2s_events, &event, 0) == pdTRUE) {
      if (event.type == I2S_EVENT_RX_Q_OVF) {
        ++overflows;
      }
    }

    const int64_t now_us = esp_timer_get_time();
    if (now_us - report_started_us >= static_cast<int64_t>(kFpsReportIntervalMs) * 1000) {
      const float elapsed_s = static_cast<float>(now_us - report_started_us) / 1000000.0F;
      Serial.printf("AUDIO_STATS kbps=%.0f rate=%.0f overflows=%u send_max_us=%lld rms=%.0f\n",
                    static_cast<double>(sent_bytes) * 8.0 / 1000.0 / elapsed_s,
                    static_cast<double>(sent_bytes) / sizeof(int16_t) / elapsed_s,
                    static_cast<unsigned>(overflows), send_max_us,
                    sample_count == 0 ? 0.0 : sqrt(square_sum / sample_count));
      sent_bytes = overflows = sample_count = 0;
      send_max_us = 0;
      square_sum = 0;
      report_started_us = now_us;
    }
  }

  i2s_stop(I2S_NUM_0);
  Serial.printf("AUDIO_CLOSED result=0x%x\n", result);
  return result;
}

void stopServers() {
  if (g_audio_server != nullptr) {
    httpd_stop(g_audio_server);
    g_audio_server = nullptr;
  }
  if (g_stream_server != nullptr) {
    httpd_stop(g_stream_server);
    g_stream_server = nullptr;
  }
  if (g_control_server != nullptr) {
    httpd_stop(g_control_server);
    g_control_server = nullptr;
  }
}

bool startServers() {
  // 이전 시도가 일부 서버만 열고 실패했을 수 있다. 남은 핸들을 정리한 뒤
  // 세 서버를 한 세트로 다시 시작한다.
  stopServers();
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

  httpd_uri_t orient_uri{};
  orient_uri.uri = "/orient";
  orient_uri.method = HTTP_GET;
  orient_uri.handler = orientHandler;
  if (httpd_start(&g_control_server, &control_config) != ESP_OK ||
      httpd_register_uri_handler(g_control_server, &status_uri) != ESP_OK ||
      httpd_register_uri_handler(g_control_server, &profile_uri) != ESP_OK ||
      httpd_register_uri_handler(g_control_server, &orient_uri) != ESP_OK) {
    Serial.println("ERROR http_server: control server start failed");
    stopServers();
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
    stopServers();
    return false;
  }

  httpd_config_t audio_config = HTTPD_DEFAULT_CONFIG();
  audio_config.server_port = kAudioPort;
  audio_config.ctrl_port = 32770;
  httpd_uri_t audio_uri{};
  audio_uri.uri = "/audio";
  audio_uri.method = HTTP_GET;
  audio_uri.handler = audioHandler;
  if (httpd_start(&g_audio_server, &audio_config) != ESP_OK ||
      httpd_register_uri_handler(g_audio_server, &audio_uri) != ESP_OK) {
    Serial.println("ERROR http_server: audio server start failed");
    stopServers();
    return false;
  }

  Serial.printf("HTTP_READY status=http://%s/ stream=http://%s:%u/stream audio=http://%s:%u/audio\n",
                WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str(), kStreamPort,
                WiFi.localIP().toString().c_str(), kAudioPort);
  return true;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("BOOT mechdog-xiao-vision link-diagnostics-v1");

  // 접속 타임아웃의 status만으로 AP 미발견·인증 실패를 구분할 수 없다.
  // 자격정보를 출력하거나 연결 정책을 바꾸지 않고 드라이버의 사유만 남긴다.
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      Serial.printf("WIFI_DISCONNECTED reason=%u\n",
                    static_cast<unsigned>(info.wifi_sta_disconnected.reason));
    } else if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
      Serial.println("WIFI_ASSOCIATED");
    }
  });

  g_camera_ready = initializeCamera();
  if (!g_camera_ready) {
    return;
  }
  // 마이크가 없어도 영상은 계속 낸다. /audio 만 503 으로 답한다.
  g_mic_ready = initializeMic();
  if (connectWifi()) {
    startServers();
  }
}

void loop() {
  if (!g_camera_ready) {
    delay(1000);
    return;
  }

  if (millis() - g_last_reconnect_ms >= kReconnectIntervalMs) {
    g_last_reconnect_ms = millis();
    if (!g_wifi_initialized) {
      connectWifi();
    } else if (WiFi.status() != WL_CONNECTED) {
      g_wifi_reported = false;
      wifi_ap_record_t access_point{};
      if (esp_wifi_sta_get_ap_info(&access_point) == ESP_OK) {
        Serial.println("WIFI_WAIT_IP");
      } else {
        // Reuse the existing configuration. Unlike begin()/reconnect(), this
        // fallback does not deliberately disconnect an associated station.
        const esp_err_t reconnect_result = esp_wifi_connect();
        Serial.printf("WIFI_RETRY result=0x%x status=%d\n", reconnect_result,
                      static_cast<int>(WiFi.status()));
      }
    }
    if (WiFi.status() == WL_CONNECTED && !g_wifi_reported) {
      Serial.printf("WIFI_READY ip=%s rssi=%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      g_wifi_reported = true;
    }
    // Wi-Fi는 살아 있는데 서버 시작만 실패한 경우도 다시 시도한다.
    if (WiFi.status() == WL_CONNECTED &&
        (g_stream_server == nullptr || g_control_server == nullptr || g_audio_server == nullptr)) {
      startServers();
    }
  }
  delay(100);
}
