/*
 * ============================================================
 *  FlowSync — Ambulance Position Beacon
 *  Runs on: ESP32 DevKit v1 (strapped to a toy ambulance)
 * ============================================================
 *
 *  In a real system, this would be a GPS tracker on the
 *  ambulance feeding live coordinates to the traffic backend.
 *  For our prototype, we simulate position with buttons:
 *
 *    Button 1 → "I'm approaching Junction 1"
 *    Button 2 → "I'm approaching Junction 2"
 *    Button 3 → "I'm approaching Junction 3"
 *    Button 4 → "I've passed all junctions / route change"
 *
 *  Press the buttons as you move the toy ambulance along the
 *  road mat. The backend picks up the position and triggers
 *  pre-clearing, warnings, and junction feedback.
 *
 *  Privacy feature: the beacon uses a rotating anonymous
 *  token that changes every 30 seconds. Public users never
 *  see a persistent ambulance identifier — they only see
 *  "emergency vehicle approaching from west, ~15s away".
 *  The operator dashboard shows the token for tracking,
 *  but even that can't be linked to a specific vehicle
 *  across token rotations.
 *
 *  Wiring:
 *    GPIO15 → Button 1 (other leg to GND) → "near J1"
 *    GPIO4  → Button 2 (other leg to GND) → "near J2"
 *    GPIO16 → Button 3 (other leg to GND) → "near J3"
 *    GPIO17 → Button 4 (other leg to GND) → "passed / reroute"
 *    GPIO2  → Built-in LED (blinks when active)
 *    All buttons use internal pull-ups. Press = LOW.
 *
 *  Libraries: WiFi, PubSubClient, ArduinoJson
 * ============================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ─── Config ─────────────────────────────────────────────────
#define WIFI_SSID    "FlowSync_Net"
#define WIFI_PASS    "flowsync123"
#define MQTT_BROKER  "192.168.1.100"
#define MQTT_PORT    1883

// ─── Pins ───────────────────────────────────────────────────
const int BTN_J1   = 15;
const int BTN_J2   = 4;
const int BTN_J3   = 16;
const int BTN_PASS = 17;   // "passed all junctions" or route change
const int LED_PIN  = 2;    // built-in LED

// ─── State ──────────────────────────────────────────────────
String currentNearest = "none";
String previousNearest = "none";
bool   evActive       = false;
String evToken        = "";
String heading        = "eastbound";  // assumed default direction

// Speed estimation: time between button presses
unsigned long lastButtonTime    = 0;
float         estimatedSpeedMps = 10.0;  // metres per second (default ~36 km/h)

// Privacy: token rotation
unsigned long lastTokenRotation = 0;
const unsigned long TOKEN_ROTATE_MS = 30000;  // new token every 30s

// Stale detection: if no button press for this long, we go inactive.
// This triggers the "stale location data" demo — warnings expire.
const unsigned long STALE_TIMEOUT_MS = 3000;
unsigned long lastButtonPress = 0;

// Debounce
const unsigned long DEBOUNCE_MS = 50;
unsigned long lastDebounce[4] = {0, 0, 0, 0};
bool lastBtnState[4] = {HIGH, HIGH, HIGH, HIGH};

// Junction positions (metres from start, for ETA calculation)
const float JUNCTION_POS[] = { 300.0, 600.0, 900.0 };  // J1, J2, J3

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// ─── Token Generation ───────────────────────────────────────
// A random 6-digit token. Changes every 30s so the public
// side can't build a movement history of this vehicle.

String generateToken() {
  char buf[10];
  snprintf(buf, sizeof(buf), "ev_%06ld", random(100000, 999999));
  return String(buf);
}

// ─── ETA Calculation ────────────────────────────────────────
// Rough estimate: given current position and speed, how long
// until we reach each junction?

float etaToJunction(int junctionIdx) {
  if (currentNearest == "none") return -1.0;

  // Where are we now? (approximate: at the nearest junction's position)
  int currentIdx = -1;
  if (currentNearest == "J1") currentIdx = 0;
  if (currentNearest == "J2") currentIdx = 1;
  if (currentNearest == "J3") currentIdx = 2;

  if (currentIdx < 0 || junctionIdx <= currentIdx) return 0.0;

  float dist = JUNCTION_POS[junctionIdx] - JUNCTION_POS[currentIdx];
  if (estimatedSpeedMps <= 0.1) return 999.0;
  return dist / estimatedSpeedMps;
}

// ─── Button Reading with Debounce ───────────────────────────

bool buttonPressed(int pin, int index) {
  bool reading = digitalRead(pin) == LOW;  // active low (pull-up)
  unsigned long now = millis();

  if (reading != lastBtnState[index]) {
    lastDebounce[index] = now;
  }
  lastBtnState[index] = reading;

  // Only register a press after stable for DEBOUNCE_MS
  if (reading && (now - lastDebounce[index]) > DEBOUNCE_MS) {
    lastDebounce[index] = now + 500;  // prevent re-trigger for 500ms
    return true;
  }
  return false;
}

// ─── Speed Estimation ───────────────────────────────────────
// When the user presses J1 → J2, we know the distance (300m)
// and the time gap, so we can estimate speed.

void updateSpeed(String from, String to) {
  unsigned long now = millis();
  if (lastButtonTime == 0) {
    lastButtonTime = now;
    return;
  }

  float timeSec = (now - lastButtonTime) / 1000.0;
  lastButtonTime = now;

  if (timeSec < 0.5) return;  // too fast, probably a double-press

  // Distance between adjacent junctions is 300m in our prototype
  float dist = 300.0;

  // Non-adjacent jumps (route change)
  if ((from == "J1" && to == "J3") || (from == "J3" && to == "J1"))
    dist = 600.0;

  estimatedSpeedMps = dist / timeSec;

  // Clamp to reasonable range (5-80 km/h → 1.4-22.2 m/s)
  if (estimatedSpeedMps < 1.4) estimatedSpeedMps = 1.4;
  if (estimatedSpeedMps > 22.2) estimatedSpeedMps = 22.2;

  Serial.printf("[SPEED] Estimated: %.1f m/s (%.0f km/h)\n",
                estimatedSpeedMps, estimatedSpeedMps * 3.6);
}

// ─── MQTT ───────────────────────────────────────────────────

void onFeedback(char* topic, byte* payload, unsigned int length) {
  // The backend tells us which junctions have secured green for us
  String msg = "";
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

  Serial.println("┌──────────────────────────────────┐");
  Serial.printf( "│  FEEDBACK: %s\n", msg.c_str());
  Serial.println("└──────────────────────────────────┘");
}

void publishPosition() {
  if (!mqtt.connected()) return;

  StaticJsonDocument<256> doc;
  doc["token"]     = evToken;
  doc["nearest"]   = currentNearest;
  doc["heading"]   = heading;
  doc["active"]    = evActive;
  doc["speed_mps"] = round(estimatedSpeedMps * 10) / 10.0;
  doc["timestamp"] = millis();

  // Include ETAs to each upcoming junction
  JsonObject etas = doc.createNestedObject("etas");
  for (int j = 0; j < 3; j++) {
    float eta = etaToJunction(j);
    if (eta > 0) {
      char key[4];
      snprintf(key, sizeof(key), "J%d", j + 1);
      etas[key] = round(eta * 10) / 10.0;
    }
  }

  char buf[256];
  serializeJson(doc, buf);
  mqtt.publish("flowsync/ev/position", buf);
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[WIFI] Connecting to %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WIFI] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WIFI] Failed. Will retry...");
  }
}

void connectMQTT() {
  if (mqtt.connected()) return;
  String clientId = "flowsync_ambulance_" + String(random(1000));
  if (mqtt.connect(clientId.c_str())) {
    Serial.println("[MQTT] Connected. Listening for feedback.");
    mqtt.subscribe("flowsync/ev/feedback");
  } else {
    Serial.printf("[MQTT] Failed (rc=%d)\n", mqtt.state());
  }
}

// ─── Setup ──────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("╔════════════════════════════════════════╗");
  Serial.println("║  FlowSync Ambulance Beacon v1.0       ║");
  Serial.println("║  ESP32 — Position & Privacy           ║");
  Serial.println("╚════════════════════════════════════════╝");
  Serial.println();
  Serial.println("Buttons:");
  Serial.println("  GPIO15 = approaching J1");
  Serial.println("  GPIO4  = approaching J2");
  Serial.println("  GPIO16 = approaching J3");
  Serial.println("  GPIO17 = passed / route change");
  Serial.println();

  pinMode(BTN_J1,   INPUT_PULLUP);
  pinMode(BTN_J2,   INPUT_PULLUP);
  pinMode(BTN_J3,   INPUT_PULLUP);
  pinMode(BTN_PASS, INPUT_PULLUP);
  pinMode(LED_PIN,  OUTPUT);

  randomSeed(analogRead(0) + millis());
  evToken = generateToken();
  Serial.printf("[PRIVACY] Initial token: %s (rotates every 30s)\n", evToken.c_str());

  connectWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(onFeedback);
  mqtt.setBufferSize(512);
  connectMQTT();

  lastButtonPress = millis();
  Serial.println("[BOOT] Beacon ready. Press a button to start broadcasting.");
}

// ─── Main Loop ──────────────────────────────────────────────

unsigned long lastPublish = 0;

void loop() {
  unsigned long now = millis();

  // Network
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  // ── Token rotation (privacy) ──
  if (now - lastTokenRotation > TOKEN_ROTATE_MS) {
    evToken = generateToken();
    lastTokenRotation = now;
    Serial.printf("[PRIVACY] Token rotated: %s\n", evToken.c_str());
  }

  // ── Read buttons ──
  previousNearest = currentNearest;

  if (buttonPressed(BTN_J1, 0)) {
    currentNearest = "J1";
    evActive = true;
    lastButtonPress = now;
    updateSpeed(previousNearest, "J1");
    Serial.println("[POS] → Approaching J1");
  }
  else if (buttonPressed(BTN_J2, 1)) {
    currentNearest = "J2";
    evActive = true;
    lastButtonPress = now;
    updateSpeed(previousNearest, "J2");
    Serial.println("[POS] → Approaching J2");
  }
  else if (buttonPressed(BTN_J3, 2)) {
    currentNearest = "J3";
    evActive = true;
    lastButtonPress = now;
    updateSpeed(previousNearest, "J3");
    Serial.println("[POS] → Approaching J3");
  }
  else if (buttonPressed(BTN_PASS, 3)) {
    Serial.println("[POS] → Passed / route complete");
    evActive = false;
    currentNearest = "past";
    lastButtonPress = now;
  }

  // ── Stale detection ──
  // If nobody's pressed a button in 3 seconds while we were active,
  // that means the "GPS" data is stale. Warnings should expire.
  if (evActive && (now - lastButtonPress > STALE_TIMEOUT_MS)) {
    evActive = false;
    Serial.println("[STALE] No position update for 3s — going inactive.");
    Serial.println("[STALE] (This triggers warning expiry on the backend.)");
  }

  // ── Publish position (every 500ms when active, every 2s when idle) ──
  unsigned long pubInterval = evActive ? 500 : 2000;
  if (now - lastPublish >= pubInterval) {
    publishPosition();
    lastPublish = now;

    // Blink LED when active
    if (evActive) {
      digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    } else {
      digitalWrite(LED_PIN, LOW);
    }

    // Debug output
    if (evActive) {
      Serial.printf("[TX] token=%s nearest=%s speed=%.1fm/s heading=%s\n",
                    evToken.c_str(), currentNearest.c_str(),
                    estimatedSpeedMps, heading.c_str());
      for (int j = 0; j < 3; j++) {
        float eta = etaToJunction(j);
        if (eta > 0) {
          Serial.printf("     ETA to J%d: %.1fs\n", j + 1, eta);
        }
      }
    }
  }
}
