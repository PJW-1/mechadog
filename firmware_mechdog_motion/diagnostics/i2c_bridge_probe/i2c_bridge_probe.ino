// WonderEcho 0x34 브리지 투명성 시험 (WBS 4.7.9 선택지 A).
//
// 4핀 뒤의 0x34 브리지가 임의 바이트를 모듈 UART1 로 넘기는지, 모듈 → 로봇 방향으로
// 무엇을 읽을 수 있는지 USB 시리얼 명령으로 직접 찔러 본다. 판정은 모듈 쪽
// `[WE-STAT] bytes=/frames=` 카운터(38-pdm)와 이 스케치의 응답을 함께 본다.
//
// 모터·Wi-Fi·벤더 라이브러리가 없다 — 서보에 명령이 가지 않으므로 서 있는 로봇은
// 주저앉는다. **배터리를 빼고 USB 전원만으로** 쓴다(4핀 5V 는 USB 전원으로 살아 있다).
// ⚠️ 업로드하면 기존 펌웨어가 덮어써진다. app0 백업을 먼저 확보할 것.
//
// 명령 (115200, 줄 단위). 주소·레지스터·데이터는 16진, 개수·시간은 10진:
//   scan                           IIC1·IIC2 주소 스캔
//   r <addr> <reg> <n> [stop]      레지스터 읽기 (n 1..32, stop=1 이면 STOP 후 읽기)
//   drain <addr> <reg> <n> <count> 같은 레지스터를 100ms 간격으로 count 번 읽기
//   w <addr> <hex>                 쓰기 (첫 바이트가 보통 레지스터, ≤32B)
//   map <addr> [stop]              reg 0x00~0xFF 를 1바이트씩 읽어 응답한 것만 출력
//   burst <addr> <hex> <interval_ms> <seconds>  같은 쓰기를 반복, 아무 키로 중단
//   unstick                        SCL 9펄스 + 버스 재초기화 (SDA 가 붙었을 때)
// 쓰기(w·burst)는 음성 모듈 브리지 0x34 와 MP3 모듈 0x7B 에만 허용한다 — 같은 버스의 IMU 0x6A·초음파 0x77 을
// 오타로 건드리지 않기 위해서다. 모든 출력 줄은 millis() 로 시작한다.

#include <Wire.h>

constexpr int kSda1 = 22;
constexpr int kScl1 = 23;
constexpr int kSda2 = 19;
constexpr int kScl2 = 13;
bool writable(uint8_t addr) { return addr == 0x34 || addr == 0x7B; }

String line;

void stamp() { Serial.printf("%lu ", static_cast<unsigned long>(millis())); }

String token(String& rest) {
  rest.trim();
  const int sp = rest.indexOf(' ');
  String t = sp < 0 ? rest : rest.substring(0, sp);
  rest = sp < 0 ? "" : rest.substring(sp + 1);
  rest.trim();
  return t;
}

bool isHex(const String& s) {
  if (s.length() == 0) return false;
  for (size_t i = 0; i < s.length(); ++i) {
    if (!isxdigit(static_cast<unsigned char>(s[i]))) return false;
  }
  return true;
}

bool isDec(const String& s) {
  if (s.length() == 0) return false;
  for (size_t i = 0; i < s.length(); ++i) {
    if (!isdigit(static_cast<unsigned char>(s[i]))) return false;
  }
  return true;
}

bool parseHexByte(const String& s, uint8_t& out) {
  if (!isHex(s) || s.length() > 2) return false;
  out = static_cast<uint8_t>(strtol(s.c_str(), nullptr, 16));
  return true;
}

bool parseAddr(const String& s, uint8_t& out) {
  return parseHexByte(s, out) && out >= 0x08 && out <= 0x7F;  // MP3 모듈이 예약 구간의 0x7B 를 쓴다
}

bool parseHexBytes(const String& s, uint8_t* out, size_t& n) {
  if (!isHex(s) || s.length() % 2 != 0 || s.length() / 2 > 32) return false;
  n = s.length() / 2;
  for (size_t i = 0; i < n; ++i) {
    out[i] = static_cast<uint8_t>(strtol(s.substring(2 * i, 2 * i + 2).c_str(), nullptr, 16));
  }
  return true;
}

// Any key stops a long-running command; the key itself is discarded so it
// does not leak into the next command line.
bool cancelled() {
  if (!Serial.available()) return false;
  delay(20);
  while (Serial.available()) Serial.read();
  stamp();
  Serial.println("cancelled");
  return true;
}

void err(const char* msg) {
  stamp();
  Serial.printf("ERR %s\n", msg);
}

void scanBus(TwoWire& bus, const char* label) {
  stamp();
  Serial.printf("[%s]", label);
  int found = 0;
  for (uint8_t a = 0x08; a < 0x80; ++a) {
    bus.beginTransmission(a);
    if (bus.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      ++found;
    }
  }
  Serial.println(found ? "" : " (none)");
}

// Returns bytes received; tx = endTransmission result of the register phase.
size_t readReg(uint8_t addr, uint8_t reg, uint8_t n, bool stop, uint8_t* buf, uint8_t& tx) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  tx = Wire.endTransmission(stop);
  if (tx != 0) return 0;
  const size_t got = Wire.requestFrom(addr, n, static_cast<uint8_t>(1));
  for (size_t i = 0; i < got; ++i) buf[i] = Wire.read();
  return got;
}

void printRead(uint8_t addr, uint8_t reg, uint8_t tx, size_t got, uint32_t us, const uint8_t* buf) {
  stamp();
  Serial.printf("R addr=0x%02X reg=0x%02X tx=%u got=%u us=%lu bytes=", addr, reg, tx,
                static_cast<unsigned>(got), static_cast<unsigned long>(us));
  for (size_t i = 0; i < got; ++i) Serial.printf("%02X", buf[i]);
  Serial.println();
}

// Parses "<addr> <reg> <n>" shared by r and drain.
bool parseReadArgs(String& rest, uint8_t& addr, uint8_t& reg, uint8_t& n) {
  const String a = token(rest), r = token(rest), c = token(rest);
  if (!parseAddr(a, addr) || !parseHexByte(r, reg) || !isDec(c)) return false;
  const long v = c.toInt();
  if (v < 1 || v > 32) return false;
  n = static_cast<uint8_t>(v);
  return true;
}

bool parseStop(String& rest, bool& stop) {
  const String s = token(rest);
  if (rest.length() || (s.length() && s != "0" && s != "1")) return false;
  stop = s == "1";
  return true;
}

void cmdRead(String rest) {
  uint8_t addr, reg, n;
  bool stop = false;
  if (!parseReadArgs(rest, addr, reg, n) || !parseStop(rest, stop)) {
    return err("usage: r <addr> <reg> <n 1..32> [stop 0|1]");
  }
  uint8_t buf[32], tx = 0;
  const uint32_t t0 = micros();
  const size_t got = readReg(addr, reg, n, stop, buf, tx);
  printRead(addr, reg, tx, got, micros() - t0, buf);
}

void cmdDrain(String rest) {
  uint8_t addr, reg, n;
  if (!parseReadArgs(rest, addr, reg, n)) return err("usage: drain <addr> <reg> <n> <count>");
  const String c = token(rest);
  if (rest.length() || !isDec(c) || c.toInt() < 1 || c.toInt() > 100) return err("count 1..100");
  for (long i = 0; i < c.toInt(); ++i) {
    if (cancelled()) break;
    uint8_t buf[32], tx = 0;
    const uint32_t t0 = micros();
    const size_t got = readReg(addr, reg, n, false, buf, tx);
    printRead(addr, reg, tx, got, micros() - t0, buf);
    delay(100);
  }
}

bool parseWriteArgs(String& rest, uint8_t& addr, uint8_t* data, size_t& n) {
  const String a = token(rest), h = token(rest);
  return parseAddr(a, addr) && parseHexBytes(h, data, n);
}

uint8_t writeBytes(uint8_t addr, const uint8_t* data, size_t n, size_t& queued) {
  Wire.beginTransmission(addr);
  queued = Wire.write(data, n);
  return Wire.endTransmission(true);
}

void cmdWrite(String rest) {
  uint8_t addr, data[32];
  size_t n = 0;
  if (!parseWriteArgs(rest, addr, data, n) || rest.length()) {
    return err("usage: w <addr> <hex, even length, <=32 bytes, no spaces>");
  }
  if (!writable(addr)) return err("writes allowed to 0x34/0x7B only");
  size_t queued = 0;
  const uint32_t t0 = micros();
  const uint8_t tx = writeBytes(addr, data, n, queued);
  stamp();
  Serial.printf("W addr=0x%02X n=%u queued=%u tx=%u us=%lu\n", addr, static_cast<unsigned>(n),
                static_cast<unsigned>(queued), tx, static_cast<unsigned long>(micros() - t0));
}

void cmdMap(String rest) {
  uint8_t addr;
  bool stop = false;
  if (!parseAddr(token(rest), addr) || !parseStop(rest, stop)) return err("usage: map <addr> [stop 0|1]");
  int answered = 0;
  for (int reg = 0; reg <= 0xFF; ++reg) {
    if (cancelled()) break;
    uint8_t b = 0, tx = 0;
    if (readReg(addr, static_cast<uint8_t>(reg), 1, stop, &b, tx) == 1) {
      stamp();
      Serial.printf("M reg=0x%02X val=0x%02X\n", reg, b);
      ++answered;
    }
    delay(5);
  }
  stamp();
  Serial.printf("M done answered=%d (n=1 reads only)\n", answered);
}

void cmdBurst(String rest) {
  uint8_t addr, data[32];
  size_t n = 0;
  if (!parseWriteArgs(rest, addr, data, n)) return err("usage: burst <addr> <hex> <interval_ms> <seconds>");
  if (!writable(addr)) return err("writes allowed to 0x34/0x7B only");
  const String iv = token(rest), sv = token(rest);
  if (rest.length() || !isDec(iv) || !isDec(sv)) return err("interval/seconds must be decimal");
  const uint32_t interval = iv.toInt(), seconds = sv.toInt();
  if (interval < 10 || interval > 1000 || seconds == 0 || seconds > 120) {
    return err("interval 10..1000 ms, seconds 1..120");
  }
  uint32_t ok = 0, nack = 0, max_us = 0;
  const uint32_t start = millis();
  uint32_t next_report = start + 1000;
  while (millis() - start < seconds * 1000) {
    if (cancelled()) break;
    size_t queued = 0;
    const uint32_t t0 = micros();
    const uint8_t tx = writeBytes(addr, data, n, queued);
    const uint32_t us = micros() - t0;
    if (us > max_us) max_us = us;
    (tx == 0 ? ok : nack)++;
    if (static_cast<int32_t>(millis() - next_report) >= 0) {
      stamp();
      Serial.printf("B progress ok=%lu nack=%lu sent_bytes=%lu\n", static_cast<unsigned long>(ok),
                    static_cast<unsigned long>(nack), static_cast<unsigned long>(ok * n));
      next_report += 1000;
    }
    delay(interval);
  }
  stamp();
  Serial.printf("B done addr=0x%02X n=%u ok=%lu nack=%lu sent_bytes=%lu max_us=%lu\n", addr,
                static_cast<unsigned>(n), static_cast<unsigned long>(ok),
                static_cast<unsigned long>(nack), static_cast<unsigned long>(ok * n),
                static_cast<unsigned long>(max_us));
}

// Clock SCL until a slave holding SDA low releases it, then restart the peripheral.
void cmdUnstick() {
  Wire.end();
  pinMode(kSda1, INPUT_PULLUP);
  pinMode(kScl1, OUTPUT_OPEN_DRAIN);
  int pulses = 0;
  for (; pulses < 9 && digitalRead(kSda1) == LOW; ++pulses) {
    digitalWrite(kScl1, LOW);
    delayMicroseconds(5);
    digitalWrite(kScl1, HIGH);
    delayMicroseconds(5);
  }
  const int sda = digitalRead(kSda1);
  Wire.begin(kSda1, kScl1, 100000);
  Wire.setTimeOut(10);
  stamp();
  Serial.printf("U pulses=%d sda_after=%d\n", pulses, sda);
}

void handle(String cmd) {
  String rest = cmd;
  const String op = token(rest);
  if (op == "scan") {
    scanBus(Wire, "IIC1");
    scanBus(Wire1, "IIC2");
  } else if (op == "r") {
    cmdRead(rest);
  } else if (op == "drain") {
    cmdDrain(rest);
  } else if (op == "w") {
    cmdWrite(rest);
  } else if (op == "map") {
    cmdMap(rest);
  } else if (op == "burst") {
    cmdBurst(rest);
  } else if (op == "unstick") {
    cmdUnstick();
  } else if (op.length()) {
    err("unknown (scan|r|drain|w|map|burst|unstick)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Wire.begin(kSda1, kScl1, 100000);
  Wire.setTimeOut(10);
  Wire1.begin(kSda2, kScl2, 100000);
  Wire1.setTimeOut(10);
  stamp();
  Serial.println("=== i2c_bridge_probe ready (no motors, no Wi-Fi) ===");
}

void loop() {
  while (Serial.available()) {
    const char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (line.length()) {
        stamp();
        Serial.printf("> %s\n", line.c_str());
        handle(line);
      }
      line = "";
    } else if (line.length() < 120) {
      line += c;
    }
  }
  delay(2);
}
