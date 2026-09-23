// XIAO ESP32S3 Sense PDM 마이크 단독 진단 (XIAO 듣기 경로 타당성 확인).
//
// ⚠️ **본 펌웨어가 아니다.** 카메라·Wi-Fi 를 켜지 않고 마이크만 본다. 업로드하면 비전
// 펌웨어가 덮어써지므로 앱 영역을 먼저 백업하고, 끝나면 되돌린다.
//
// 확인하는 것: 확장보드의 PDM 마이크(CLK=GPIO42, DATA=GPIO41)로 16 kHz 16-bit 모노를
// 녹음해 PC 로 넘기면, 사람 말소리가 Whisper 가 알아들을 만한 품질인가.
// 카메라와 동시에 돌렸을 때의 fps 영향은 이 단계가 통과한 다음에 따로 본다.
//
// 명령 (USB CDC, 줄 단위):
//   rec <ms>   ms 동안(100..10000) 녹음 → "DATA <bytes>\n" + PCM16LE 원시 바이트 + "\nEND\n"
//   gain <n>   왼쪽 시프트 이득 0..4 (기본 2). Seeed 예제의 VOLUME_GAIN 과 같은 뜻이다.
// 녹음 직후 "STAT rms= peak= dc=" 를 먼저 찍는다 — 파일을 열지 않고도 무음·포화를 가린다.
// Board: esp32:esp32:XIAO_ESP32S3:PSRAM=opi (코어 2.0.17, 비전 펌웨어와 같은 환경)

#include <Arduino.h>
#include <I2S.h>

namespace {

constexpr int kPinPdmClk = 42;
constexpr int kPinPdmData = 41;
constexpr int kSampleRate = 16000;
constexpr uint32_t kMaxMs = 10000;

int g_gain = 2;
String g_line;

void record(uint32_t ms) {
  const size_t samples = static_cast<size_t>(kSampleRate) * ms / 1000;
  const size_t bytes = samples * sizeof(int16_t);
  auto* buf = static_cast<int16_t*>(ps_malloc(bytes));
  if (!buf) {
    Serial.println("ERR ps_malloc");
    return;
  }
  // The first ~100 ms after start carry the PDM filter settling; discard them.
  size_t got = 0;
  int16_t scratch[256];
  for (int i = 0; i < 6; ++i) esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, scratch, sizeof(scratch), &got, portMAX_DELAY);

  size_t filled = 0;
  while (filled < bytes) {
    esp_i2s::i2s_read(esp_i2s::I2S_NUM_0, reinterpret_cast<uint8_t*>(buf) + filled, bytes - filled, &got,
                      portMAX_DELAY);
    filled += got;
  }

  int64_t sum = 0;
  for (size_t i = 0; i < samples; ++i) sum += buf[i];
  const int32_t dc = static_cast<int32_t>(sum / static_cast<int64_t>(samples));
  double sq = 0;
  int32_t peak = 0;
  for (size_t i = 0; i < samples; ++i) {
    int32_t v = (static_cast<int32_t>(buf[i]) - dc) << g_gain;
    v = constrain(v, -32768, 32767);
    buf[i] = static_cast<int16_t>(v);
    sq += static_cast<double>(v) * v;
    if (abs(v) > peak) peak = abs(v);
  }
  Serial.printf("STAT ms=%lu rms=%.1f peak=%ld dc=%ld gain=%d\n", static_cast<unsigned long>(ms),
                sqrt(sq / samples), static_cast<long>(peak), static_cast<long>(dc), g_gain);
  Serial.printf("DATA %u\n", static_cast<unsigned>(bytes));
  Serial.write(reinterpret_cast<const uint8_t*>(buf), bytes);
  Serial.print("\nEND\n");
  free(buf);
}

void handle(const String& cmd) {
  if (cmd.startsWith("rec ")) {
    const long ms = cmd.substring(4).toInt();
    if (ms < 100 || ms > static_cast<long>(kMaxMs)) return (void)Serial.println("ERR rec 100..10000");
    record(static_cast<uint32_t>(ms));
  } else if (cmd.startsWith("gain ")) {
    const long g = cmd.substring(5).toInt();
    if (g < 0 || g > 4) return (void)Serial.println("ERR gain 0..4");
    g_gain = static_cast<int>(g);
    Serial.printf("OK gain=%d\n", g_gain);
  } else if (cmd.length()) {
    Serial.println("ERR unknown (rec <ms> | gain <n>)");
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);
  I2S.setAllPins(-1, kPinPdmClk, kPinPdmData, -1, -1);
  if (!I2S.begin(PDM_MONO_MODE, kSampleRate, 16)) {
    Serial.println("ERR i2s_begin");
    return;
  }
  Serial.printf("MIC_READY rate=%d psram_free=%u\n", kSampleRate, static_cast<unsigned>(ESP.getFreePsram()));
}

void loop() {
  while (Serial.available()) {
    const char c = Serial.read();
    if (c == '\n' || c == '\r') {
      handle(g_line);
      g_line = "";
    } else if (g_line.length() < 40) {
      g_line += c;
    }
  }
  delay(2);
}
