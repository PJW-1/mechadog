// IMU 칩 확정 및 데이터 읽기 확인 (WBS 2.1.3 · OI-23).
//
// `i2c_scan` 이 `IIC1` 의 `0x6A` 에서 응답을 받았고, 순정 MicroPython 펌웨어가 부팅 로그에
// `QMI8658 init.` 을 찍었다. 그러나 **`0x6A` 는 QMI8658 과 LSM6DS 계열이 공유하는 주소**라
// 로그만으로는 확정이 아니다. `WHO_AM_I` 를 직접 읽어 확정한다.
//
//   QMI8658   reg 0x00 -> 0x05
//   LSM6DS    reg 0x0F -> 0x6A(DSL) · 0x6B(DSR) · 0x6C(DSO/DSOX) 등
//   MPU6050   reg 0x75 -> 0x68   (벤더 예제가 전제하는 칩)
//
// 확정 다음으로 **실제 가속도가 읽히는지**까지 본다. 그래야 `NFR-2.4` 전도 감지가
// 이 경로로 성립한다고 말할 수 있다 — 칩이 있다는 것과 읽을 수 있다는 것은 다르다.
//
//   arduino-cli compile --fqbn esp32:esp32:esp32 firmware_mechdog_motion/diagnostics/imu_probe
//   arduino-cli upload -p COM9 --fqbn esp32:esp32:esp32 firmware_mechdog_motion/diagnostics/imu_probe
//
// ⚠️ 업로드하면 기존 펌웨어가 덮어써진다. 되돌릴 이미지를 먼저 확보할 것.

#include <Wire.h>

// 벤더 `Hiwonder.h` 의 `IIC1`. 값을 바꾸려면 그 파일과 함께 바꾼다.
constexpr int kSda1 = 22;
constexpr int kScl1 = 23;

constexpr uint8_t kImuAddr = 0x6A;  // i2c_scan 실측값
constexpr uint8_t kMpuAddr = 0x68;  // 벤더 예제가 부르는 주소

// QMI8658 레지스터 (데이터시트)
constexpr uint8_t kWhoAmI = 0x00;
constexpr uint8_t kRevision = 0x01;
constexpr uint8_t kCtrl2 = 0x03;  // 가속도 범위·출력속도
constexpr uint8_t kCtrl7 = 0x08;  // 센서 활성화
constexpr uint8_t kStatus0 = 0x2E;
constexpr uint8_t kAxL = 0x35;  // AX_L 부터 6바이트가 XYZ

// 배터리 ADC — 벤더 `Hiwonder.cpp` 가 `analogRead(34) * 3.6` 으로 읽는다.
// 삐 소리(저전압 경고)의 원인을 확인하려면 이 값이 필요하다. 2S 리튬이므로
// 정상 7.4V, 경고 7.0V, 셧다운 6.6V 다 (`config.safety`).
constexpr int kBatteryAdcPin = 34;

bool readReg(uint8_t addr, uint8_t reg, uint8_t* out) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, (uint8_t)1) != 1) return false;
  *out = Wire.read();
  return true;
}

bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

void identify() {
  Serial.println("\n-- 칩 확정 (WHO_AM_I) --");
  uint8_t v = 0;
  if (readReg(kImuAddr, kWhoAmI, &v)) {
    Serial.printf("  0x6A reg0x00 = 0x%02X  %s\n", v,
                  v == 0x05 ? "→ QMI8658 확정" : "→ QMI8658 아님");
  } else {
    Serial.println("  0x6A reg0x00 읽기 실패");
  }
  if (readReg(kImuAddr, kRevision, &v)) Serial.printf("  0x6A reg0x01 = 0x%02X (revision)\n", v);
  if (readReg(kImuAddr, 0x0F, &v)) {
    Serial.printf("  0x6A reg0x0F = 0x%02X  %s\n", v,
                  (v == 0x6A || v == 0x6B || v == 0x6C) ? "→ LSM6DS 가능성" : "(LSM6DS 아님)");
  }
  Serial.printf("  0x68 응답: %s  ← 벤더 예제가 부르는 주소\n",
                readReg(kMpuAddr, 0x75, &v) ? "있음" : "없음");
}

void readAccel() {
  Serial.println("\n-- 가속도 읽기 (QMI8658 기준) --");
  // aFS ±4g · aODR 약 250Hz. 정확한 스케일은 드라이버 구현 시 확정한다.
  if (!writeReg(kImuAddr, kCtrl2, 0x24) || !writeReg(kImuAddr, kCtrl7, 0x01)) {
    Serial.println("  설정 쓰기 실패 — 이 경로로는 못 읽는다");
    return;
  }
  delay(200);
  uint8_t st = 0;
  readReg(kImuAddr, kStatus0, &st);
  Serial.printf("  STATUS0 = 0x%02X (bit0 = 데이터 준비)\n", st);

  for (int i = 0; i < 8; ++i) {
    uint8_t raw[6] = {0};
    bool ok = true;
    for (int b = 0; b < 6; ++b) ok &= readReg(kImuAddr, kAxL + b, &raw[b]);
    if (!ok) {
      Serial.println("  데이터 레지스터 읽기 실패");
      return;
    }
    int16_t ax = (int16_t)(raw[1] << 8 | raw[0]);
    int16_t ay = (int16_t)(raw[3] << 8 | raw[2]);
    int16_t az = (int16_t)(raw[5] << 8 | raw[4]);
    Serial.printf("  ax=%6d  ay=%6d  az=%6d\n", ax, ay, az);
    delay(150);
  }
  Serial.println("  ↑ 값이 0 이 아니고 기울이면 변하면 읽기 성립");
}

void readBattery() {
  Serial.println("\n-- 배터리 전압 (GPIO34) --");
  for (int i = 0; i < 5; ++i) {
    const int raw = analogRead(kBatteryAdcPin);
    // 벤더 공식 그대로 — mV 로 나오므로 1000 으로 나눈다.
    const float volts = raw * 3.6f / 1000.0f;
    const char* verdict = volts > 7.0f   ? "정상"
                          : volts > 6.6f ? "경고 — 저전압 부저가 울릴 구간"
                                         : "셧다운 구간";
    Serial.printf("  raw=%4d  약 %.2f V  %s\n", raw, volts, verdict);
    delay(200);
  }
  Serial.println("  ↑ 벤더 공식(analogRead*3.6)을 그대로 썼다. 보정은 2.1.3 에서 한다");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== MechDog IMU · 배터리 확인 (OI-23 · WBS 2.1.3) ===");
  Wire.begin(kSda1, kScl1, 100000);
  identify();
  readAccel();
  readBattery();
  Serial.println("\n=== 끝 ===");
}

void loop() {
  delay(1000);
}
