// 눈 LED 탐색 — 초음파 모듈에 내장된 RGB LED 의 레지스터를 실물에서 확정한다 (WBS 4.7.3).
//
// **왜 필요한가.** 눈 LED 는 별도 부품이 아니라 초음파 센서(I2C 0x77)에 내장돼 있다
// (아키텍처 3.1 · OI-12 닫힘). 그런데 그 모듈의 RGB 레지스터 번호가 저장소 어디에도 없고,
// 벤더 배포본도 순정 MicroPython 백업도 이 PC 에 없다. 거리값을 0x00 에서 2바이트로 읽는
// 것만 확정돼 있다(src/sensor_hal.cpp). **추측으로 본 펌웨어를 고칠 수 없으므로 실물에서
// 찾는다.** 사람이 눈을 보고 있어야 하는 시험이다.
//
// **무엇을 쓰는가.** Hiwonder 계열 모듈에서 통용되는 배치를 첫 가설로 잡았다.
//   0x02      동작 모드 (0 = 사용자 지정 색, 1 = 호흡등)
//   0x03~0x05 LED1 의 R, G, B
//   0x06~0x08 LED2 의 R, G, B
// 가설이 맞으면 아래 순서대로 빨강·초록·파랑·흰색이 보인다. 하나도 안 보이면 가설이 틀린
// 것이므로 그 사실을 기록하고 다음 후보로 넘어간다.
//
// ⚠️ **0x09 이상은 건드리지 않는다.** 모듈에 따라 주소 변경이나 영구 설정 레지스터가 그
// 근처에 있을 수 있고, 잘못 쓰면 모듈을 못 쓰게 만든다. 범위를 넓히는 것은 자료를 확보한
// 뒤에 한다.
//
// 이 스케치는 서보도 Wi-Fi 도 건드리지 않는다. 저장소만으로 빌드된다.
//
//   & "C:\Program Files\Arduino CLI\arduino-cli.exe" compile --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" firmware_mechdog_motion/diagnostics/led_probe
//   & "C:\Program Files\Arduino CLI\arduino-cli.exe" upload -p COM8 --fqbn "esp32:esp32:esp32:FlashMode=dio,FlashFreq=40" firmware_mechdog_motion/diagnostics/led_probe
//
// ⚠️ 업로드하면 운용 펌웨어가 덮어써진다. 되돌릴 이미지를 먼저 확보할 것.

#include <Wire.h>

namespace {

// 벤더 `Hiwonder.h` 가 정의한 IIC1. 값을 바꾸려면 그 파일과 함께 바꾼다.
constexpr int kSda = 22;
constexpr int kScl = 23;

constexpr uint8_t kSonarAddress = 0x77;

constexpr uint8_t kRegMode = 0x02;
constexpr uint8_t kRegLed1R = 0x03;

constexpr unsigned long kHoldMs = 3000;

struct Color {
  const char* name;
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

// 규약의 다섯 색(blue·yellow·orange·red·white)을 먼저 보고, 원색 셋으로 채널 순서를 가린다.
const Color kColors[] = {
    {"RED    (R255 G0   B0  )", 255, 0, 0},   {"GREEN  (R0   G255 B0  )", 0, 255, 0},
    {"BLUE   (R0   G0   B255)", 0, 0, 255},   {"YELLOW (R255 G255 B0  )", 255, 255, 0},
    {"ORANGE (R255 G128 B0  )", 255, 128, 0}, {"WHITE  (R255 G255 B255)", 255, 255, 255},
    {"OFF    (R0   G0   B0  )", 0, 0, 0},
};

bool write_reg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(kSonarAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

void scan_bus() {
  Serial.println("-- I2C 스캔 (SDA22 / SCL23) --");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; ++addr) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("   응답: 0x%02X\n", addr);
      ++found;
    }
  }
  Serial.printf("   합계 %d개\n", found);
}

// 거리 2바이트 읽기. 모듈이 살아 있는지 보는 독립 근거다.
void read_distance() {
  Wire.beginTransmission(kSonarAddress);
  Wire.write(0x00);
  if (Wire.endTransmission() != 0) {
    Serial.println("-- 거리 읽기: 레지스터 지정 실패 --");
    return;
  }
  if (Wire.requestFrom(static_cast<int>(kSonarAddress), 2) != 2) {
    Serial.println("-- 거리 읽기: 응답 없음 --");
    return;
  }
  const uint8_t low = Wire.read();
  const uint8_t high = Wire.read();
  const uint16_t mm = static_cast<uint16_t>(low) | (static_cast<uint16_t>(high) << 8);
  Serial.printf("-- 거리 읽기: raw=0x%02X%02X (%u mm) --\n", high, low, mm);
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println("LED_PROBE: 시작 — 초음파 모듈의 RGB 레지스터를 찾는다");

  Wire.begin(kSda, kScl);
  Wire.setClock(100000);

  scan_bus();
  read_distance();

  Serial.printf("LED_PROBE: 모드 레지스터 0x%02X 에 0 을 쓴다 (사용자 지정 색)\n", kRegMode);
  Serial.println(write_reg(kRegMode, 0) ? "   쓰기 ACK 받음"
                                        : "   쓰기 실패 — 모듈이 이 주소에 없다");
}

void loop() {
  for (const Color& c : kColors) {
    // LED 두 개를 같은 색으로 맞춘다. 0x03~0x05 가 LED1, 0x06~0x08 이 LED2 라는 가설이다.
    const bool ok1 = write_reg(kRegLed1R + 0, c.r) && write_reg(kRegLed1R + 1, c.g) &&
                     write_reg(kRegLed1R + 2, c.b);
    const bool ok2 = write_reg(kRegLed1R + 3, c.r) && write_reg(kRegLed1R + 4, c.g) &&
                     write_reg(kRegLed1R + 5, c.b);
    Serial.printf("LED_PROBE: %s  쓰기=%s\n", c.name, (ok1 && ok2) ? "OK" : "실패");
    delay(kHoldMs);
  }
  Serial.println("LED_PROBE: 한 바퀴 끝 — 다시 시작한다");
}
