// I2C 버스 스캔 — 실제로 어떤 센서가 달려 있는지 확인한다 (WBS 2.1.3).
//
// **왜 필요한가.** 벤더 Arduino 예제는 IMU 를 `MPU6050_ADDRESS 0x68` 로 하드코딩하는데,
// 순정 MicroPython 펌웨어는 부팅 로그에 `QMI8658 init.` 을 찍는다. 둘 중 하나가 틀렸거나
// 보드에 둘 다 있다. **추측으로 정할 수 없으므로 주소를 직접 읽는다.**
//
// 이 판정이 중요한 이유 — 전도 감지(NFR-2.4)가 IMU 에 의존하고, 그것은 넘어진 로봇의
// 서보가 스톨로 기어를 파손하는 것을 막는 유일한 장치다. IMU 를 못 읽으면 Tier 1 안전
// 로직 하나가 성립하지 않는다.
//
// 벤더 라이브러리를 쓰지 않으므로 이 스케치는 저장소만으로 빌드된다.
//
//   arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion/diagnostics/i2c_scan
//   arduino-cli upload -p COM9 --fqbn esp32:esp32:esp32 firmware_mechdog_motion/diagnostics/i2c_scan
//
// ⚠️ 업로드하면 기존 펌웨어가 덮어써진다. 되돌릴 이미지를 먼저 확보할 것.

#include <Wire.h>

// 벤더 `Hiwonder.h` 가 정의한 두 버스. 값을 바꾸려면 그 파일과 함께 바꾼다.
constexpr int kSda1 = 22;
constexpr int kScl1 = 23;
constexpr int kSda2 = 19;
constexpr int kScl2 = 13;

struct Known {
  uint8_t addr;
  const char* name;
};

// 주소만으로 칩을 확정할 수는 없다 — 겹치는 부품이 있다. 후보를 보여주고 판단은 사람이 한다.
constexpr Known kKnown[] = {
    {0x68, "MPU6050 / QMI8658(SA0=0) 후보"},
    {0x69, "MPU6050(AD0=1) 후보"},
    {0x6A, "QMI8658(SA0=1) / LSM6DS 후보"},
    {0x6B, "QMI8658 / LSM6DS 후보"},
    {0x77, "Glowy 초음파 모듈 (벤더 정의 ULTRASOUND_I2C_ADDR)"},
};

void scan(TwoWire& bus, const char* label, int sda, int scl) {
  Serial.printf("\n[%s] SDA=%d SCL=%d\n", label, sda, scl);
  bus.begin(sda, scl, 100000);
  int found = 0;
  for (uint8_t addr = 0x08; addr < 0x78; ++addr) {
    bus.beginTransmission(addr);
    if (bus.endTransmission() != 0) continue;
    ++found;
    const char* note = "";
    for (const Known& k : kKnown) {
      if (k.addr == addr) note = k.name;
    }
    Serial.printf("  0x%02X  %s\n", addr, note);
  }
  if (found == 0) Serial.println("  (응답 없음)");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== MechDog I2C 스캔 ===");
  Serial.println(
      "IMU 후보 주소 — MPU6050: 0x68/0x69 · QMI8658: 0x6A/0x6B (SA0 에 따라 0x68 도 가능)");
  scan(Wire, "IIC1", kSda1, kScl1);
  scan(Wire1, "IIC2", kSda2, kScl2);
  Serial.println("\n=== 끝 ===");
}

void loop() {
  delay(1000);
}
