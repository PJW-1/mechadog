// XIAO Wi-Fi 단독 확인 (카메라와 분리).
//
// ⚠️ **본 펌웨어는 카메라 초기화에 실패하면 Wi-Fi 접속까지 가지 않는다**
// (`setup()` 이 그 자리에서 return 한다). 그래서 카메라가 고장 난 상태에서는
// *"공유기 정보가 맞는가"* 를 확인할 수단이 없다 — 이 스케치가 그 수단이다.
//
// 하는 일은 셋이다.
//   ① 주변 AP 를 훑어 대상 SSID 가 보이는지, 2.4GHz 인지 확인한다
//   ② `wifi_secrets.h` 의 값으로 접속하고 IP·RSSI 를 출력한다
//   ③ 끊기면 드라이버의 사유 코드를 그대로 남긴다
//
// ⚠️ **SSID·비밀번호를 출력하지 않는다.** 본 펌웨어와 같은 규칙이다. 실패를
// 구분하는 데 필요한 것은 자격정보가 아니라 드라이버의 사유 코드다.

#include <Arduino.h>
#include <WiFi.h>

#include "../../wifi_secrets.h"

namespace {

constexpr uint32_t kConnectTimeoutMs = 20000;

const char* authName(wifi_auth_mode_t mode) {
  switch (mode) {
    case WIFI_AUTH_OPEN: return "OPEN";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA";
    case WIFI_AUTH_WPA2_PSK: return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
    case WIFI_AUTH_WPA3_PSK: return "WPA3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default: return "기타";
  }
}

void scan() {
  Serial.println("\n--- ① AP 스캔 ---");
  const int found = WiFi.scanNetworks();
  if (found <= 0) {
    Serial.println("  ⚠️ AP 를 하나도 못 봤다 — 안테나나 무선부를 의심한다");
    return;
  }
  bool target_seen = false;
  for (int i = 0; i < found; ++i) {
    const bool is_target = WiFi.SSID(i) == String(MECHDOG_WIFI_SSID);
    if (is_target) {
      target_seen = true;
    }
    // ⚠️ 채널 14 이하가 2.4GHz 다. XIAO 는 5GHz 를 보지 못한다.
    if (is_target || i < 8) {
      Serial.printf("  %-28s ch=%2d rssi=%4d %s%s\n", WiFi.SSID(i).c_str(), WiFi.channel(i),
                    WiFi.RSSI(i), authName(WiFi.encryptionType(i)),
                    is_target ? "   ← 대상" : "");
    }
  }
  Serial.printf("  대상 SSID %s\n",
                target_seen ? "발견" : "⚠️ 안 보임 — 2.4GHz 인지, 이름이 정확한지 확인한다");
  WiFi.scanDelete();
}

void connect() {
  Serial.println("\n--- ② 접속 ---");
  WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
      Serial.printf("  WIFI_DISCONNECTED reason=%u\n",
                    static_cast<unsigned>(info.wifi_sta_disconnected.reason));
    } else if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
      Serial.println("  WIFI_ASSOCIATED");
    }
  });

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // 본 펌웨어와 같은 설정 (왕복 지연 55.8ms → 3.6ms · PR #23)
  WiFi.begin(MECHDOG_WIFI_SSID, MECHDOG_WIFI_PASSWORD);

  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < kConnectTimeoutMs) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("  WIFI_READY ip=%s rssi=%d ch=%d\n", WiFi.localIP().toString().c_str(),
                  WiFi.RSSI(), WiFi.channel());
    Serial.printf("  gateway=%s subnet=%s\n", WiFi.gatewayIP().toString().c_str(),
                  WiFi.subnetMask().toString().c_str());
  } else {
    Serial.printf("  ⚠️ 접속 실패 status=%d\n", static_cast<int>(WiFi.status()));
    Serial.println("  status 만으로 비밀번호 오류를 단정하지 않는다 — 위 reason 코드를 본다");
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n==== XIAO Wi-Fi 진단 (카메라 무관) ====");
  scan();
  connect();
  Serial.println("\n==== 진단 끝 ====");
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last >= 5000) {
    last = millis();
    Serial.printf("status=%d ip=%s rssi=%d\n", static_cast<int>(WiFi.status()),
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  }
  delay(100);
}
