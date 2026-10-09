// XIAO ESP32S3 Sense 카메라 배선 진단 (WBS 4.2.1 장애 분석용).
//
// ⚠️ **본 펌웨어가 아니다.** `esp_camera_init` 이 실패할 때 원인을 좁히는 도구다.
//
// **경과 (2026-09-18)**
//   낮:  `0x3C CHIP_ID=0x3660` → OV3660 확인, `esp_camera_init` 통과, VGA 프레임 수신
//   이후: 보조배터리가 저전류로 출력을 끊은 뒤부터 `0x3C` 가 한 번도 응답하지 않는다.
//         리본 재장착 뒤에도 같다.
//
// 그래서 이 판은 **«센서가 대답하지 않는다» 의 아래 단계**를 본다.
//   ① SDA·SCL 이 풀업으로 올라오는가 — 신호선이 물려 있기는 한가
//   ② 클럭 없이 / 클럭 켜고, 느린 속도로도 응답하는가
//   ③ 전 주소 스캔 — 0x3C 가 아닌 다른 주소로 잡히지는 않는가
//
// ⚠️ **①이 핵심이다.** 리본이 빠져 있으면 내부 풀업만 남아 HIGH 로 읽히지만,
// 센서가 붙어 있으면 모듈의 풀업 저항이 함께 걸린다. 둘을 직접 구분할 수는 없어도,
// **LOW 로 눌려 있으면 배선 단락이나 센서가 버스를 잡고 있는 상태**라 원인이 갈린다.

#include <Arduino.h>
#include <Wire.h>
#include <esp_camera.h>
#include <esp_log.h>

namespace {

constexpr int kPinXclk = 10;
constexpr int kPinSiod = 40;  // SDA
constexpr int kPinSioc = 39;  // SCL
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

void checkLines() {
  Serial.println("\n--- ① 신호선 상태 ---");

  pinMode(kPinSiod, INPUT);
  pinMode(kPinSioc, INPUT);
  delay(5);
  Serial.printf("  풀업 없이  SDA=%d SCL=%d  (떠 있으면 값이 흔들린다)\n", digitalRead(kPinSiod),
                digitalRead(kPinSioc));

  pinMode(kPinSiod, INPUT_PULLUP);
  pinMode(kPinSioc, INPUT_PULLUP);
  delay(5);
  const int sda = digitalRead(kPinSiod);
  const int scl = digitalRead(kPinSioc);
  Serial.printf("  내부 풀업  SDA=%d SCL=%d\n", sda, scl);
  if (sda == 0 || scl == 0) {
    Serial.println("  ⚠️ 풀업을 걸었는데 LOW 다 — 단락이거나 상대가 버스를 잡고 있다");
  } else {
    Serial.println("  풀업에서 HIGH — 선이 접지로 눌려 있지는 않다");
  }

  // 데이터 선도 함께 본다. 리본이 통째로 빠졌는지 일부만 떴는지의 단서가 된다.
  const int data_pins[] = {kPinD0, kPinD1, kPinD2, kPinD3, kPinD4, kPinD5, kPinD6, kPinD7};
  Serial.print("  데이터선(D0~D7) 풀업: ");
  for (int pin : data_pins) {
    pinMode(pin, INPUT_PULLUP);
  }
  delay(5);
  for (int pin : data_pins) {
    Serial.print(digitalRead(pin));
  }
  Serial.println();
  Serial.printf("  VSYNC=%d HREF=%d PCLK=%d\n", digitalRead(kPinVsync), digitalRead(kPinHref),
                digitalRead(kPinPclk));
}

void scanBus(const char* label, bool with_clock, uint32_t speed) {
  Serial.printf("\n--- ② %s (%lu Hz) ---\n", label, (unsigned long)speed);
  if (with_clock) {
    if (!ledcAttach(kPinXclk, 20000000, 1)) {
      Serial.println("  ⚠️ ledcAttach 실패");
      return;
    }
    ledcWrite(kPinXclk, 1);
    delay(100);
  }

  Wire.begin(kPinSiod, kPinSioc, speed);
  delay(50);
  int found = 0;
  for (uint8_t addr = 1; addr < 127; ++addr) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  응답 0x%02X\n", addr);
      ++found;
    }
  }
  if (found == 0) {
    Serial.println("  응답 없음");
  }
  Wire.end();

  if (with_clock) {
    ledcDetach(kPinXclk);
  }
  delay(100);
}

void tryInit() {
  Serial.println("\n--- ③ esp_camera_init ---");
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
  config.pin_pwdn = -1;
  config.pin_reset = -1;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.fb_count = 2;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.grab_mode = CAMERA_GRAB_LATEST;

  const esp_err_t error = esp_camera_init(&config);
  Serial.printf("  → 0x%x (%s)\n", error, esp_err_to_name(error));
  if (error == ESP_OK) {
    sensor_t* s = esp_camera_sensor_get();
    Serial.printf("  ✅ PID=0x%04X\n", s ? s->id.PID : 0);
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb != nullptr) {
      Serial.printf("  프레임 %ux%u · %u 바이트\n", fb->width, fb->height, (unsigned)fb->len);
      esp_camera_fb_return(fb);
    }
  }
  esp_camera_deinit();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n==== XIAO 카메라 배선 진단 ====");
  Serial.printf("PSRAM: %s\n", psramFound() ? "있음" : "⚠️ 없음");

  esp_log_level_set("camera", ESP_LOG_VERBOSE);

  checkLines();
  scanBus("클럭 없이", false, 100000);
  scanBus("클럭 켜고", true, 100000);
  scanBus("클럭 켜고 · 느리게", true, 50000);
  tryInit();

  Serial.println("\n==== 진단 끝 ====");
}

void loop() {
  delay(5000);
}
