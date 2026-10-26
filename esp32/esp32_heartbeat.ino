/*
 * ESP32 Heartbeat — sends a signal to the server every 30 seconds.
 * When power goes out, ESP32 shuts off → heartbeat stops → bot notifies.
 *
 * Setup:
 *   1. Fill in your WiFi credentials and server IP below
 *   2. Flash to ESP32 via Arduino IDE
 *   3. Plug ESP32 into a wall outlet (NO UPS!) via USB charger
 */

#include <WiFi.h>
#include <HTTPClient.h>

// ═══ CONFIGURE THESE ═══
const char* WIFI_SSID       = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD   = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL      = "http://YOUR_SERVER_IP:8080/heartbeat";
const char* SECRET_KEY      = "YOUR_SECRET_KEY";
// ════════════════════════

const unsigned long HEARTBEAT_INTERVAL = 30000;  // 30 seconds
const int LED_PIN = 2;
unsigned long lastHeartbeat = 0;
int failCount = 0;

void setup() {
    Serial.begin(115200);
    pinMode(LED_PIN, OUTPUT);
    Serial.println("\n🔌 ESP32 Power Monitor Heartbeat");
    connectWiFi();
}

void loop() {
    if (WiFi.status() != WL_CONNECTED) connectWiFi();
    if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL) {
        sendHeartbeat();
        lastHeartbeat = millis();
    }
    delay(100);
}

void connectWiFi() {
    Serial.printf("📡 %s", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int a = 0;
    while (WiFi.status() != WL_CONNECTED && a < 60) {
        delay(500); Serial.print("."); a++;
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n✅ IP: %s\n", WiFi.localIP().toString().c_str());
        digitalWrite(LED_PIN, HIGH);
    } else {
        Serial.println("\n❌ WiFi fail"); delay(10000); ESP.restart();
    }
}

void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    HTTPClient http;
    http.begin(SERVER_URL);
    http.addHeader("X-Secret", SECRET_KEY);
    http.setTimeout(10000);
    int code = http.GET();
    if (code == 200) {
        failCount = 0; digitalWrite(LED_PIN, HIGH);
        static int c = 0;
        if (++c % 10 == 0) Serial.printf("💓 #%d OK | %ddBm\n", c, WiFi.RSSI());
    } else {
        failCount++;
        Serial.printf("⚠️ HTTP %d (fail %d)\n", code, failCount);
        if (failCount >= 10) { delay(1000); ESP.restart(); }
    }
    http.end();
}
