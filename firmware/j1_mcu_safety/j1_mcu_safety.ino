/*
 * ============================================================
 *  FlowSync — Junction 1 MCU Safety Controller
 *  Runs on: Arduino UNO Q — STM32U585 (Cortex-M33, 160 MHz)
 * ============================================================
 *
 *  This is the safety-critical layer. It owns the LEDs and
 *  enforces every signal-timing rule in hardware. The Linux
 *  side (Python backend) can *propose* phase changes through
 *  the RPC bridge, but this sketch has the final say.
 *
 *  If the Linux side crashes, goes silent, or sends nonsense,
 *  the MCU falls back to a safe fixed-time cycle on its own.
 *
 *  Why this matters: real traffic controllers work exactly
 *  this way — the safety logic runs on a dedicated processor
 *  that the application layer cannot bypass.
 *
 *  Wiring (all pins need a 330Ω resistor to the LED anode):
 *    North: D2=Red  D3=Yellow  D4=Green
 *    South: D5=Red  D6=Yellow  D7=Green
 *    East:  D8=Red  D9=Yellow  D10=Green
 *    West:  D11=Red D12=Yellow D13=Green
 *    All cathodes → GND rail
 *
 *  UNO Q GPIOs are 3.3V. Keep each LED under 8 mA.
 *  330Ω gives roughly (3.3 − 2.0) / 330 ≈ 4 mA — safe and visible.
 * ============================================================
 */

// ─── Pin Map ────────────────────────────────────────────────
//                    Red  Yel  Grn
const int NORTH[] = {  2,   3,   4 };
const int SOUTH[] = {  5,   6,   7 };
const int EAST[]  = {  8,   9,  10 };
const int WEST[]  = { 11,  12,  13 };

const int* APPROACHES[] = { NORTH, SOUTH, EAST, WEST };
const char* APPROACH_NAMES[] = { "N", "S", "E", "W" };
const int NUM_APPROACHES = 4;
const int LEDS_PER_APPROACH = 3;  // R, Y, G

// ─── Phase Definitions ─────────────────────────────────────
// A traffic signal cycle always follows this order:
//   EW_GREEN → EW_YELLOW → ALL_RED → NS_GREEN → NS_YELLOW → ALL_RED
// You cannot skip steps. That's the whole point of this sketch.

enum Phase {
  EW_GREEN   = 0,
  EW_YELLOW  = 1,
  ALL_RED_1  = 2,
  NS_GREEN   = 3,
  NS_YELLOW  = 4,
  ALL_RED_2  = 5,
  PHASE_COUNT = 6
};

const char* PHASE_NAMES[] = {
  "EW_GREEN", "EW_YELLOW", "ALL_RED_1",
  "NS_GREEN", "NS_YELLOW", "ALL_RED_2"
};

// Which approaches get green in each phase?
//                          N  S  E  W
const bool GREEN[PHASE_COUNT][4] = {
  /* EW_GREEN  */ { 0, 0, 1, 1 },
  /* EW_YELLOW */ { 0, 0, 0, 0 },
  /* ALL_RED_1 */ { 0, 0, 0, 0 },
  /* NS_GREEN  */ { 1, 1, 0, 0 },
  /* NS_YELLOW */ { 0, 0, 0, 0 },
  /* ALL_RED_2 */ { 0, 0, 0, 0 },
};

// Which approaches get yellow (transitional)?
const bool YELLOW[PHASE_COUNT][4] = {
  /* EW_GREEN  */ { 0, 0, 0, 0 },
  /* EW_YELLOW */ { 0, 0, 1, 1 },
  /* ALL_RED_1 */ { 0, 0, 0, 0 },
  /* NS_GREEN  */ { 0, 0, 0, 0 },
  /* NS_YELLOW */ { 1, 1, 0, 0 },
  /* ALL_RED_2 */ { 0, 0, 0, 0 },
};

// ─── Safety Limits (non-negotiable) ────────────────────────
const unsigned long MIN_GREEN_MS     = 10000;   // 10s — pedestrians need this
const unsigned long MAX_GREEN_MS     = 60000;   // 60s — don't starve cross traffic
const unsigned long YELLOW_MS        = 3000;    // 3s — drivers need reaction time
const unsigned long ALL_RED_MS       = 2000;    // 2s — let the intersection clear
const unsigned long DEFAULT_GREEN_MS = 25000;   // 25s — fixed-time fallback
const unsigned long WATCHDOG_MS      = 5000;    // 5s — if Linux is silent, take over

// ─── Runtime State ──────────────────────────────────────────
Phase currentPhase       = EW_GREEN;
unsigned long phaseStart = 0;
unsigned long greenDur   = DEFAULT_GREEN_MS;  // current green duration (adjustable)

// Watchdog: tracks whether the Linux side is alive
unsigned long lastLinuxHeartbeat = 0;
bool linuxAlive                  = false;
bool wasLinuxAlive               = false;      // for detecting transitions

// Pending request from Linux (queued for the next legal green)
bool     hasPendingRequest  = false;
Phase    pendingGreenPhase  = EW_GREEN;
unsigned long pendingGreenDur = DEFAULT_GREEN_MS;

// Stats (useful for the operator dashboard)
unsigned long phaseChangeCount = 0;
unsigned long rejectionCount   = 0;
unsigned long watchdogTrips    = 0;

// ─── LED Control ────────────────────────────────────────────

void setLED(const int pins[], bool red, bool yellow, bool green) {
  digitalWrite(pins[0], red    ? HIGH : LOW);
  digitalWrite(pins[1], yellow ? HIGH : LOW);
  digitalWrite(pins[2], green  ? HIGH : LOW);
}

void applyPhase(Phase p) {
  for (int i = 0; i < NUM_APPROACHES; i++) {
    if (GREEN[p][i]) {
      setLED(APPROACHES[i], false, false, true);
    } else if (YELLOW[p][i]) {
      setLED(APPROACHES[i], false, true, false);
    } else {
      setLED(APPROACHES[i], true, false, false);
    }
  }
}

// Quick visual test on startup — blink every LED so you can
// verify your wiring before the system goes live.
void startupTest() {
  Serial.println(F("[BOOT] Running LED self-test..."));
  for (int color = 0; color < 3; color++) {
    for (int i = 0; i < NUM_APPROACHES; i++) {
      digitalWrite(APPROACHES[i][color], HIGH);
    }
    delay(300);
    for (int i = 0; i < NUM_APPROACHES; i++) {
      digitalWrite(APPROACHES[i][color], LOW);
    }
    delay(150);
  }
  Serial.println(F("[BOOT] LED test complete."));
}

// ─── Conflict Detection ────────────────────────────────────
// This should never trigger in normal operation because the
// phase table is correct. But if a bug somehow creates a
// conflicting state, this catches it before anyone gets hurt.

bool hasConflict(Phase p) {
  bool ns = GREEN[p][0] || GREEN[p][1];
  bool ew = GREEN[p][2] || GREEN[p][3];
  return ns && ew;  // North-South and East-West both green = disaster
}

// ─── Phase Machine ─────────────────────────────────────────

Phase nextPhase(Phase current) {
  // Always follow the fixed sequence. No shortcuts.
  switch (current) {
    case EW_GREEN:  return EW_YELLOW;
    case EW_YELLOW: return ALL_RED_1;
    case ALL_RED_1: return NS_GREEN;
    case NS_GREEN:  return NS_YELLOW;
    case NS_YELLOW: return ALL_RED_2;
    case ALL_RED_2: return EW_GREEN;
    default:        return EW_GREEN;  // shouldn't happen, but be safe
  }
}

unsigned long phaseDuration(Phase p) {
  switch (p) {
    case EW_GREEN:
    case NS_GREEN:  return greenDur;
    case EW_YELLOW:
    case NS_YELLOW: return YELLOW_MS;
    case ALL_RED_1:
    case ALL_RED_2: return ALL_RED_MS;
    default:        return DEFAULT_GREEN_MS;
  }
}

void transitionTo(Phase p) {
  // Final safety gate — if anything looks wrong, go all-red
  if (hasConflict(p)) {
    Serial.println(F("[SAFETY] CONFLICT DETECTED — forcing ALL_RED"));
    p = ALL_RED_1;
  }

  currentPhase = p;
  phaseStart = millis();
  applyPhase(p);
  phaseChangeCount++;

  Serial.print(F("[PHASE] → "));
  Serial.print(PHASE_NAMES[p]);
  Serial.print(F("  dur="));
  Serial.print(phaseDuration(p));
  Serial.print(F("ms  mode="));
  Serial.print(linuxAlive ? F("adaptive") : F("fallback"));
  Serial.print(F("  #"));
  Serial.println(phaseChangeCount);
}

// ─── RPC Handlers (called from Linux via the bridge) ───────

// Linux sends: "EW_GREEN:30000" meaning "please switch to
// East-West green for 30 seconds". We validate and queue it.
String handleProposal(String request) {
  lastLinuxHeartbeat = millis();  // any command counts as a heartbeat

  int sep = request.indexOf(':');
  if (sep < 0) {
    rejectionCount++;
    Serial.println(F("[RPC] Rejected: bad format (expected PHASE:DURATION)"));
    return "rejected:bad_format";
  }

  String phaseStr = request.substring(0, sep);
  unsigned long dur = request.substring(sep + 1).toInt();

  // Figure out which phase they want
  Phase requested;
  if      (phaseStr == "EW_GREEN") requested = EW_GREEN;
  else if (phaseStr == "NS_GREEN") requested = NS_GREEN;
  else {
    rejectionCount++;
    Serial.print(F("[RPC] Rejected: unknown phase '"));
    Serial.print(phaseStr);
    Serial.println(F("'"));
    return "rejected:unknown_phase";
  }

  // ── Safety checks ──

  if (dur < MIN_GREEN_MS) {
    rejectionCount++;
    Serial.println(F("[RPC] Rejected: below minimum green (10s)"));
    return "rejected:below_min_green";
  }
  if (dur > MAX_GREEN_MS) {
    rejectionCount++;
    Serial.println(F("[RPC] Rejected: above maximum green (60s)"));
    return "rejected:above_max_green";
  }

  // You can't jump straight from one green to a conflicting green.
  // The caller needs to wait — we'll handle the transition ourselves.
  if ((currentPhase == EW_GREEN && requested == NS_GREEN) ||
      (currentPhase == NS_GREEN && requested == EW_GREEN)) {
    // That's fine — we'll queue it for after yellow + all-red
  }

  // Accept: queue for the next legal transition point
  pendingGreenPhase = requested;
  pendingGreenDur   = dur;
  hasPendingRequest = true;

  Serial.print(F("[RPC] Accepted: "));
  Serial.print(phaseStr);
  Serial.print(F(" for "));
  Serial.print(dur);
  Serial.println(F("ms (queued)"));

  return "accepted";
}

// Linux can ask "what's happening right now?"
String handleGetState() {
  lastLinuxHeartbeat = millis();

  // Return a simple JSON string the Python side can parse
  String json = "{";
  json += "\"phase\":\"" + String(PHASE_NAMES[currentPhase]) + "\",";
  json += "\"elapsed\":" + String(millis() - phaseStart) + ",";
  json += "\"duration\":" + String(phaseDuration(currentPhase)) + ",";
  json += "\"mode\":\"" + String(linuxAlive ? "adaptive" : "fallback") + "\",";
  json += "\"linux_alive\":" + String(linuxAlive ? "true" : "false") + ",";
  json += "\"changes\":" + String(phaseChangeCount) + ",";
  json += "\"rejections\":" + String(rejectionCount) + ",";
  json += "\"watchdog_trips\":" + String(watchdogTrips);
  json += "}";
  return json;
}

// ─── Serial Command Parser (backup if RPC bridge isn't working) ──
// Send "CMD:EW_GREEN:25000\n" from the Serial Monitor to test.
// Send "STATE\n" to get the current state.

void checkSerialCommands() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();

  if (line.startsWith("CMD:")) {
    String payload = line.substring(4);
    String result = handleProposal(payload);
    Serial.print(F("[SERIAL] Result: "));
    Serial.println(result);
  }
  else if (line == "STATE") {
    Serial.println(handleGetState());
  }
  else if (line == "HELP") {
    Serial.println(F("Commands:"));
    Serial.println(F("  CMD:EW_GREEN:25000  — propose East-West green for 25s"));
    Serial.println(F("  CMD:NS_GREEN:20000  — propose North-South green for 20s"));
    Serial.println(F("  STATE               — get current state as JSON"));
    Serial.println(F("  HELP                — this message"));
  }
  else {
    Serial.print(F("[SERIAL] Unknown: '"));
    Serial.print(line);
    Serial.println(F("'. Type HELP."));
  }
}

// ─── Setup ──────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println(F(""));
  Serial.println(F("╔══════════════════════════════════════════╗"));
  Serial.println(F("║   FlowSync MCU Safety Controller v1.0   ║"));
  Serial.println(F("║   Junction 1 — Arduino UNO Q STM32      ║"));
  Serial.println(F("╚══════════════════════════════════════════╝"));
  Serial.println(F(""));

  // Set up all LED pins as outputs
  for (int i = 0; i < NUM_APPROACHES; i++) {
    for (int j = 0; j < LEDS_PER_APPROACH; j++) {
      pinMode(APPROACHES[i][j], OUTPUT);
      digitalWrite(APPROACHES[i][j], LOW);
    }
  }

  startupTest();

  // Try to initialize the RPC bridge. If it fails (e.g., running
  // standalone for testing), we just fall back to serial commands.
  // Uncomment these two lines when deploying on the actual UNO Q:
  //
  // Bridge.begin();
  // Bridge.provide_safe("propose_phase", handleProposal);
  // Bridge.provide_safe("get_state", handleGetState);

  // Start in EW_GREEN
  greenDur = DEFAULT_GREEN_MS;
  phaseStart = millis();
  lastLinuxHeartbeat = millis();
  transitionTo(EW_GREEN);

  Serial.println(F("[BOOT] Safety controller running."));
  Serial.println(F("[BOOT] Type HELP for serial commands."));
  Serial.println(F(""));
}

// ─── Main Loop ──────────────────────────────────────────────

void loop() {
  unsigned long now = millis();

  // ── Watchdog: is the Linux side still talking to us? ──
  linuxAlive = (now - lastLinuxHeartbeat) < WATCHDOG_MS;

  if (wasLinuxAlive && !linuxAlive) {
    // Linux just went silent — switch to fixed-time fallback
    watchdogTrips++;
    greenDur = DEFAULT_GREEN_MS;
    hasPendingRequest = false;
    Serial.println(F("[WATCHDOG] Linux went silent. Reverting to fixed-time cycle."));
  }
  else if (!wasLinuxAlive && linuxAlive) {
    Serial.println(F("[WATCHDOG] Linux is back online. Accepting proposals."));
  }
  wasLinuxAlive = linuxAlive;

  // ── Phase timing ──
  unsigned long elapsed = now - phaseStart;
  unsigned long dur = phaseDuration(currentPhase);

  if (elapsed >= dur) {
    // Time's up for this phase — figure out what's next
    Phase next = nextPhase(currentPhase);

    // If Linux requested a specific green phase, honour it at
    // the right moment (during the all-red before the green).
    // This is the only place where external requests take effect.
    if (hasPendingRequest) {
      if (currentPhase == ALL_RED_1 && pendingGreenPhase == NS_GREEN) {
        next = NS_GREEN;
        greenDur = pendingGreenDur;
        hasPendingRequest = false;
        Serial.println(F("[PHASE] Honouring pending request: NS_GREEN"));
      }
      else if (currentPhase == ALL_RED_2 && pendingGreenPhase == EW_GREEN) {
        next = EW_GREEN;
        greenDur = pendingGreenDur;
        hasPendingRequest = false;
        Serial.println(F("[PHASE] Honouring pending request: EW_GREEN"));
      }
      else if (currentPhase == ALL_RED_1 && pendingGreenPhase == EW_GREEN) {
        // They want EW but we're about to go NS — skip NS and go back
        // Wait, we can't skip NS entirely or cross traffic starves.
        // Give NS a minimum green, then honour EW next cycle.
        next = NS_GREEN;
        greenDur = MIN_GREEN_MS;  // minimum courtesy green for cross traffic
        Serial.println(F("[PHASE] Pending EW — giving NS minimum green first"));
      }
      else if (currentPhase == ALL_RED_2 && pendingGreenPhase == NS_GREEN) {
        next = EW_GREEN;
        greenDur = MIN_GREEN_MS;
        Serial.println(F("[PHASE] Pending NS — giving EW minimum green first"));
      }
    } else if (!linuxAlive) {
      // No pending request and Linux is dead — use fixed timing
      greenDur = DEFAULT_GREEN_MS;
    }

    transitionTo(next);
  }

  // ── Check for serial commands (backup interface) ──
  checkSerialCommands();

  // ── Heartbeat (every 5s, just a serial message) ──
  static unsigned long lastHeartbeat = 0;
  if (now - lastHeartbeat > 5000) {
    Serial.print(F("[♥] phase="));
    Serial.print(PHASE_NAMES[currentPhase]);
    Serial.print(F(" elapsed="));
    Serial.print(elapsed / 1000);
    Serial.print(F("s/"));
    Serial.print(dur / 1000);
    Serial.print(F("s mode="));
    Serial.println(linuxAlive ? F("adaptive") : F("FALLBACK"));
    lastHeartbeat = now;
  }
}
