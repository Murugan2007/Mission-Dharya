/*
 * ============================================================
 *  FlowSync — ESP32 Junction Controller
 *  Runs on: ESP32 DevKit v1 (WROOM-32)
 *  For: Junction 2 or Junction 3 (change the #define below)
 * ============================================================
 *
 *  Each ESP32 junction is a mini traffic controller that:
 *    1. Drives 12 LEDs (4 approaches × R/Y/G)
 *    2. Reads 2 IR obstacle sensors for queue detection
 *    3. Receives phase commands from the backend via MQTT
 *    4. Reports queue data and health status back
 *    5. Sounds a buzzer when an EV warning arrives
 *    6. Falls back to fixed timing if MQTT goes silent
 *
 *  Even though the backend tells us what to do, we enforce
 *  the same safety rules locally. If the backend asks us to
 *  jump from EW_GREEN to NS_GREEN, we'll transition safely
 *  through yellow and all-red first. Trust but verify.
 *
 *  Libraries needed (install via Arduino Library Manager):
 *    - PubSubClient by Nick O'Leary  (MQTT)
 *    - ArduinoJson by Benoît Blanchon (JSON)
 *    - WiFi (built into ESP32 core)
 *
 *  Wiring (all LEDs need a 330Ω resistor, 3.3V logic):
 *    North: GPIO13=Red  GPIO12=Yellow  GPIO14=Green
 *    South: GPIO27=Red  GPIO26=Yellow  GPIO25=Green
 *    East:  GPIO33=Red  GPIO32=Yellow  GPIO23=Green
 *    West:  GPIO22=Red  GPIO21=Yellow  GPIO19=Green
 *    IR East:  GPIO36 (input only, no pullup available)
 *    IR West:  GPIO39 (input only, no pullup available)
 *    Buzzer:   GPIO18
 * ============================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ╔══════════════════════════════════════════════════════════╗
// ║  CHANGE THESE FOR YOUR SETUP                            ║
// ╚══════════════════════════════════════════════════════════╝
#define JUNCTION_ID   "J2"              // "J2" or "J3"
#define WIFI_SSID     "FlowSync_Net"    // your WiFi network
#define WIFI_PASS     "flowsync123"     // your WiFi password
#define MQTT_BROKER   "192.168.1.100"   // UNO Q's IP address
#define MQTT_PORT     1883

// ─── Pin Map ────────────────────────────────────────────────
//                        Red  Yel  Grn
const int NORTH_PINS[] = { 13,  12,  14 };
const int SOUTH_PINS[] = { 27,  26,  25 };
const int EAST_PINS[]  = { 33,  32,  23 };
const int WEST_PINS[]  = { 22,  21,  19 };

const int* APPROACHES[] = { NORTH_PINS, SOUTH_PINS, EAST_PINS, WEST_PINS };
const char* APPROACH_NAMES[] = { "N", "S", "E", "W" };

const int IR_EAST_PIN = 36;   // VP — input only
const int IR_WEST_PIN = 39;   // VN — input only
const int BUZZER_PIN  = 18;

// ─── Phase Definitions (same as J1, safety is consistent) ──
enum Phase {
  EW_GREEN = 0, EW_YELLOW, ALL_RED_1,
  NS_GREEN, NS_YELLOW, ALL_RED_2,
  PHASE_COUNT
};

const char* PHASE_NAMES[] = {
  "EW_GREEN", "EW_YELLOW", "ALL_RED_1",
  "NS_GREEN", "NS_YELLOW", "ALL_RED_2"
};

const bool GREEN_MAP[PHASE_COUNT][4] = {
  {0,0,1,1}, {0,0,0,0}, {0,0,0,0},
  {1,1,0,0}, {0,0,0,0}, {0,0,0,0}
};
const bool YELLOW_MAP[PHASE_COUNT][4] = {
  {0,0,0,0}, {0,0,1,1}, {0,0,0,0},
  {0,0,0,0}, {1,1,0,0}, {0,0,0,0}
};

// ─── Safety Limits ──────────────────────────────────────────
const unsigned long MIN_GREEN_MS     = 10000;
const unsigned long MAX_GREEN_MS     = 60000;
const unsigned long YELLOW_MS        = 3000;
const unsigned long ALL_RED_MS       = 2000;
const unsigned long DEFAULT_GREEN_MS = 25000;
const unsigned long MQTT_WATCHDOG_MS = 10000;  // 10s without MQTT → fallback

// ─── Runtime State ──────────────────────────────────────────
Phase currentPhase     = EW_GREEN;
unsigned long phaseStart = 0;
unsigned long greenDur = DEFAULT_GREEN_MS;

Phase targetGreen      = EW_GREEN;   // what the backend wants next
bool hasTarget         = false;
unsigned long targetDur = DEFAULT_GREEN_MS;

unsigned long lastMqttMsg  = 0;
bool mqttAlive             = false;
String currentMode         = "fixed_fallback";  // until first MQTT msg

// Sensor health tracking
int irEastHistory[60];
int irWestHistory[60];
int sensorHistIdx  = 0;
bool eastSensorOk  = true;
bool westSensorOk  = true;

// Buzzer state
unsigned long buzzerStart = 0;
int buzzerBeeps           = 0;
bool buzzerState          = false;
unsigned long lastBuzzerToggle = 0;

// MQTT
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

// ─── LED Control ────────────────────────────────────────────

void setLED(const int pins[], bool r, bool y, bool g) {
  digitalWrite(pins[0], r ? HIGH : LOW);
  digitalWrite(pins[1], y ? HIGH : LOW);
  digitalWrite(pins[2], g ? HIGH : LOW);
}

void applyPhase(Phase p) {
  for (int i = 0; i < 4; i++) {
    if (GREEN_MAP[p][i])       setLED(APPROACHES[i], false, false, true);
    else if (YELLOW_MAP[p][i]) setLED(APPROACHES[i], false, true, false);
    else                       setLED(APPROACHES[i], true, false, false);
  }
}

void startupTest() {
  Serial.println("[BOOT] LED self-test...");
  for (int c = 0; c < 3; c++) {
    for (int i = 0; i < 4; i++) digitalWrite(APPROACHES[i][c], HIGH);
    delay(250);
    for (int i = 0; i < 4; i++) digitalWrite(APPROACHES[i][c], LOW);
    delay(100);
  }
  Serial.println("[BOOT] LEDs OK.");
}

// ─── Phase Machine ─────────────────────────────────────────

Phase nextPhase(Phase current) {
  switch (current) {
    case EW_GREEN:  return EW_YELLOW;
    case EW_YELLOW: return ALL_RED_1;
    case ALL_RED_1: return NS_GREEN;
    case NS_GREEN:  return NS_YELLOW;
    case NS_YELLOW: return ALL_RED_2;
    case ALL_RED_2: return EW_GREEN;
    default:        return EW_GREEN;
  }
}

unsigned long phaseDuration(Phase p) {
  if (p == EW_GREEN || p == NS_GREEN) return greenDur;
  if (p == EW_YELLOW || p == NS_YELLOW) return YELLOW_MS;
  return ALL_RED_MS;
}

void transitionTo(Phase p) {
  currentPhase = p;
  phaseStart = millis();
  applyPhase(p);
  Serial.printf("[PHASE] → %s  dur=%lums  mode=%s\n",
                PHASE_NAMES[p], phaseDuration(p), currentMode.c_str());
}

// ─── MQTT Callbacks ─────────────────────────────────────────

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  lastMqttMsg = millis();
  mqttAlive = true;
  currentMode = "adaptive";

  // Parse the JSON payload
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.printf("[MQTT] JSON parse error: %s\n", err.c_str());
    return;
  }

  String t = String(topic);

  // ── Phase command ──
  if (t.endsWith("/phase")) {
    String phaseStr = doc["phase"] | "unknown";
    unsigned long dur = doc["duration"] | DEFAULT_GREEN_MS;

    // Clamp duration to safe range
    if (dur < MIN_GREEN_MS) dur = MIN_GREEN_MS;
    if (dur > MAX_GREEN_MS) dur = MAX_GREEN_MS;

    Phase requested;
    if      (phaseStr == "EW_GREEN") requested = EW_GREEN;
    else if (phaseStr == "NS_GREEN") requested = NS_GREEN;
    else {
      Serial.printf("[MQTT] Unknown phase: %s\n", phaseStr.c_str());
      return;
    }

    // Queue the request — we'll handle it at the right moment
    targetGreen = requested;
    targetDur   = dur;
    hasTarget   = true;
    Serial.printf("[MQTT] Queued: %s for %lums\n", phaseStr.c_str(), dur);
  }

  // ── EV warning ──
  else if (t.endsWith("/warning")) {
    bool active = doc["active"] | false;
    const char* msg = doc["msg"] | "";

    if (active) {
      Serial.printf("[WARNING] ⚠ %s\n", msg);
      buzzerBeeps = 5;     // 5 beep pattern
      buzzerStart = millis();
      buzzerState = false;
      lastBuzzerToggle = millis();
    } else {
      Serial.printf("[WARNING] ✓ Cleared: %s\n", msg);
      // Single short confirmation beep
      digitalWrite(BUZZER_PIN, HIGH);
      delay(100);
      digitalWrite(BUZZER_PIN, LOW);
      buzzerBeeps = 0;
    }
  }
}

// ─── WiFi Management ────────────────────────────────────────

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("[WIFI] Connecting to %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WIFI] Connected! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WIFI] Failed — will retry in loop.");
  }
}

// ─── MQTT Management ────────────────────────────────────────

void connectMQTT() {
  if (mqtt.connected()) return;

  String clientId = String("flowsync_") + JUNCTION_ID + "_" + String(random(1000));
  Serial.printf("[MQTT] Connecting as %s...\n", clientId.c_str());

  if (mqtt.connect(clientId.c_str())) {
    Serial.println("[MQTT] Connected to broker.");

    // Subscribe to our topics
    String phaseTopic = String("flowsync/") + JUNCTION_ID + "/phase";
    String warnTopic  = String("flowsync/") + JUNCTION_ID + "/warning";
    mqtt.subscribe(phaseTopic.c_str());
    mqtt.subscribe(warnTopic.c_str());
    Serial.printf("[MQTT] Subscribed: %s, %s\n", phaseTopic.c_str(), warnTopic.c_str());
  } else {
    Serial.printf("[MQTT] Failed (rc=%d). Will retry...\n", mqtt.state());
  }
}

// ─── Sensor Reading & Health ────────────────────────────────

void readSensors() {
  int east = digitalRead(IR_EAST_PIN) == LOW ? 1 : 0;  // LOW = obstacle detected
  int west = digitalRead(IR_WEST_PIN) == LOW ? 1 : 0;

  // Track history for stuck-sensor detection
  irEastHistory[sensorHistIdx] = east;
  irWestHistory[sensorHistIdx] = west;
  sensorHistIdx = (sensorHistIdx + 1) % 60;

  // A sensor is "stuck" if it's been the same value for 60 consecutive reads.
  // This catches unplugged sensors or hardware failures.
  if (sensorHistIdx == 0) {  // check every full cycle
    bool eastAllSame = true, westAllSame = true;
    for (int i = 1; i < 60; i++) {
      if (irEastHistory[i] != irEastHistory[0]) eastAllSame = false;
      if (irWestHistory[i] != irWestHistory[0]) westAllSame = false;
    }
    if (eastAllSame != !eastSensorOk || westAllSame != !westSensorOk) {
      // Status changed — worth logging
      if (eastAllSame && eastSensorOk) {
        eastSensorOk = false;
        Serial.println("[SENSOR] ⚠ East IR sensor appears stuck — check wiring");
      } else if (!eastAllSame && !eastSensorOk) {
        eastSensorOk = true;
        Serial.println("[SENSOR] ✓ East IR sensor recovered");
      }
      if (westAllSame && westSensorOk) {
        westSensorOk = false;
        Serial.println("[SENSOR] ⚠ West IR sensor appears stuck — check wiring");
      } else if (!westAllSame && !westSensorOk) {
        westSensorOk = true;
        Serial.println("[SENSOR] ✓ West IR sensor recovered");
      }
    }
  }
}

void publishQueue() {
  if (!mqtt.connected()) return;

  int east = digitalRead(IR_EAST_PIN) == LOW ? 1 : 0;
  int west = digitalRead(IR_WEST_PIN) == LOW ? 1 : 0;

  StaticJsonDocument<128> doc;
  doc["east"] = east;
  doc["west"] = west;
  doc["east_ok"] = eastSensorOk;
  doc["west_ok"] = westSensorOk;

  char buf[128];
  serializeJson(doc, buf);

  String topic = String("flowsync/") + JUNCTION_ID + "/queue";
  mqtt.publish(topic.c_str(), buf);
}

void publishStatus() {
  if (!mqtt.connected()) return;

  StaticJsonDocument<192> doc;
  doc["mode"] = currentMode;
  doc["sensor_ok"] = eastSensorOk && westSensorOk;
  doc["east_sensor_ok"] = eastSensorOk;
  doc["west_sensor_ok"] = westSensorOk;
  doc["phase"] = PHASE_NAMES[currentPhase];
  doc["uptime"] = millis() / 1000;

  char buf[192];
  serializeJson(doc, buf);

  String topic = String("flowsync/") + JUNCTION_ID + "/status";
  mqtt.publish(topic.c_str(), buf);
}

// ─── Buzzer Pattern ─────────────────────────────────────────
// EV warning: 200ms on, 200ms off, repeated 5 times
// Feels urgent without being annoying

void updateBuzzer() {
  if (buzzerBeeps <= 0) {
    digitalWrite(BUZZER_PIN, LOW);
    return;
  }

  unsigned long now = millis();
  if (now - lastBuzzerToggle >= 200) {
    buzzerState = !buzzerState;
    digitalWrite(BUZZER_PIN, buzzerState ? HIGH : LOW);
    lastBuzzerToggle = now;

    if (!buzzerState) {
      buzzerBeeps--;  // count down on each off-cycle
    }
  }
}

// ─── Setup ──────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("╔════════════════════════════════════════╗");
  Serial.printf( "║  FlowSync Junction %s Controller     ║\n", JUNCTION_ID);
  Serial.println("║  ESP32 DevKit v1                      ║");
  Serial.println("╚════════════════════════════════════════╝");
  Serial.println();

  // LED pins
  for (int i = 0; i < 4; i++)
    for (int j = 0; j < 3; j++)
      pinMode(APPROACHES[i][j], OUTPUT);

  // Sensor pins (36 and 39 are input-only, no need for mode set,
  // but let's be explicit)
  pinMode(IR_EAST_PIN, INPUT);
  pinMode(IR_WEST_PIN, INPUT);

  // Buzzer
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  // Init sensor history to varying values so we don't false-alarm
  for (int i = 0; i < 60; i++) {
    irEastHistory[i] = i % 2;
    irWestHistory[i] = i % 2;
  }

  startupTest();

  // Network
  connectWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(512);
  connectMQTT();

  // Start with EW_GREEN
  phaseStart = millis();
  lastMqttMsg = millis();
  transitionTo(EW_GREEN);

  Serial.printf("[BOOT] %s ready. Broker: %s:%d\n", JUNCTION_ID, MQTT_BROKER, MQTT_PORT);
}

// ─── Main Loop ──────────────────────────────────────────────

unsigned long lastQueuePub  = 0;
unsigned long lastStatusPub = 0;

void loop() {
  unsigned long now = millis();

  // ── Network maintenance ──
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  // ── MQTT watchdog ──
  bool wasAlive = mqttAlive;
  mqttAlive = (now - lastMqttMsg) < MQTT_WATCHDOG_MS;
  if (wasAlive && !mqttAlive) {
    currentMode = "fixed_fallback";
    hasTarget = false;
    greenDur = DEFAULT_GREEN_MS;
    Serial.println("[WATCHDOG] No MQTT for 10s — reverting to fixed-time.");
  }

  // ── Phase timing ──
  unsigned long elapsed = now - phaseStart;
  unsigned long dur = phaseDuration(currentPhase);

  if (elapsed >= dur) {
    Phase next = nextPhase(currentPhase);

    // Honour pending target at the right moment
    if (hasTarget) {
      if ((currentPhase == ALL_RED_1 && targetGreen == NS_GREEN) ||
          (currentPhase == ALL_RED_2 && targetGreen == EW_GREEN)) {
        next = targetGreen;
        greenDur = targetDur;
        hasTarget = false;
      }
      else if (currentPhase == ALL_RED_1 && targetGreen == EW_GREEN) {
        next = NS_GREEN;
        greenDur = MIN_GREEN_MS;
      }
      else if (currentPhase == ALL_RED_2 && targetGreen == NS_GREEN) {
        next = EW_GREEN;
        greenDur = MIN_GREEN_MS;
      }
    } else if (!mqttAlive) {
      greenDur = DEFAULT_GREEN_MS;
    }

    transitionTo(next);
  }

  // ── Sensor reading (every 500ms) ──
  static unsigned long lastSensorRead = 0;
  if (now - lastSensorRead > 500) {
    readSensors();
    lastSensorRead = now;
  }

  // ── Publish queue data (every 2s) ──
  if (now - lastQueuePub > 2000) {
    publishQueue();
    lastQueuePub = now;
  }

  // ── Publish status (every 5s) ──
  if (now - lastStatusPub > 5000) {
    publishStatus();
    lastStatusPub = now;
  }

  // ── Buzzer animation ──
  updateBuzzer();
}
