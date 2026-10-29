# Mission Dharya -> FlowSync: AI-Powered City Traffic Intelligence & Coordination

> **A connected, multi-intersection traffic management prototype that flushes congestion ahead of emergency vehicles before they arrive with hardware-enforced safety and driver privacy by design.**

Built for **Problem Statement 3 (Hardware Domain)**.

---

##  The Problem Nobody Talks About

Standard Emergency Vehicle Preemption (EVP) systems from basic optical sensors to modern GPS cloud triggers suffer from a glaring physical reality:

> **Turning a traffic light green is completely useless if 8 cars are stopped in front of the ambulance with nowhere to go.**

When emergency vehicles approach an intersection during peak congestion, cross-traffic might stop, but the ambulance remains trapped behind queued cars on its own approach. Drivers panic, try to squeeze into gutters, or run red lights illegally.

### What FlowSync Does Differently:
1. **Proactive Pre-Clearing**: Instead of abruptly turning lights green at the last second, FlowSync calculates the ambulance’s ETA and the queue depth downstream. It extends the green phase early to flush out waiting cars *before* the ambulance gets there.
2. **Targeted, Privacy-First Warnings**: Motorists on affected approaches receive advance warnings (*"Ambulance approaching from west in ~15s. Make way when safe and lawful"*). Warnings auto-cancel when the vehicle passes or if the GPS data goes stale. Rotating 30-second anonymous tokens prevent public tracking of emergency routes.
3. **Hardware-Enforced Deterministic Safety**: The AI reasoning layer (LLM) **never** directly controls the signal lights. Signal changes are proposed to an independent microcontroller (STM32 on the Arduino UNO Q) which enforces minimum greens, yellow intervals, and non-conflicting phases in silicon. If the Linux side or network drops, it fails safe immediately.

---

##  System Architecture

FlowSync uses a dual-layer architecture separating **high-level advisory AI** from **real-time deterministic hardware control**:

```
                              ┌─────────────────────────────────────────────────────────────┐
                              │            Arduino UNO Q (4GB RAM / 32GB eMMC)              │
                              │                                                             │
                              │   [Debian Linux Side - Cortex-A53 @ 2.0 GHz]                │
                              │    • Flask Backend with Public & Operator Roles             │
                              │    • Local Mosquitto MQTT Broker (port 1883)                │
                              │    • OpenCV USB Webcam Queue Detector (/dev/video0)         │
                              │    • LLM Tool Orchestrator (Gemini 2.0 + Deterministic Tools)│
                              │    • Audit Log & Privacy Token Rotator                      │
                              │                         ▲                                   │
                              │                         │ RPC Bridge (Propose / Validate)   │
                              │                         ▼                                   │
                              │   [MCU Real-Time Safety Core - STM32U585 @ 160 MHz]         │
                              │    • 12 Signal LEDs for Junction 1 (N, S, E, W)             │
                              │    • Enforces: 10s Min Green, 3s Yellow, 2s All-Red         │
                              │    • Hardware conflict detector (Never simultaneous greens) │
                              │    • Watchdog fallback (5s silence → fixed 25s cycle)       │
                              └────────────────────────▲─────────────────────────▲──────────┘
                                                       │                         │
                                            Wi-Fi MQTT │              Wi-Fi MQTT │
                                                       ▼                         ▼
                                         ┌─────────────────────────┐ ┌─────────────────────────┐
                                         │   Junction 2 (ESP32)    │ │   Junction 3 (ESP32)    │
                                         │    • 12 Signal LEDs     │ │    • 12 Signal LEDs     │
                                         │    • 2x IR Queue Sensors│ │    • 2x IR Queue Sensors│
                                         │    • Local Safety FSM   │ │    • Local Safety FSM   │
                                         │    • Warning Buzzer     │ │    • Warning Buzzer     │
                                         └─────────────────────────┘ └─────────────────────────┘
                                                                   ▲
                                                        Wi-Fi MQTT │ 500ms Position Beacon
                                                                   │
                                                     ┌───────────────────────────┐
                                                     │  Ambulance Beacon (ESP32) │
                                                     │    • Push-button GPS Sim  │
                                                     │    • 30s Rotating Tokens  │
                                                     │    • Closed-Loop Status   │
                                                     └───────────────────────────┘
```

---

##  Bill of Materials (BOM)

To build this physical prototype on a tabletop road mat (~60 × 40 cm), you will need:

| Category | Component | Qty | Role |
|---|---|:---:|---|
| **Controllers** | **Arduino UNO Q (4GB / 32GB)** | 1 | Junction 1 controller, MQTT broker, web server & safety MCU |
| | **ESP32 DevKit v1** (WROOM-32) | 3 | Junction 2, Junction 3, and Ambulance beacon |
| **Sensing** | **USB Webcam (UVC compliant)** | 1 | Overhead camera at Junction 1 for OpenCV queue detection |
| | **IR Obstacle Sensors (FC-51)** | 4 to 6 | Road-level queue detection for Junctions 2 & 3 |
| **Signals & Audio** | **5mm LEDs (Red, Yellow, Green)** | 12 each (36 total) | 4 approaches × 3 colours × 3 junctions |
| | **330Ω Resistors (1/4W)** | 36 | 1 per LED (Current limiting at 3.3V) |
| | **Active Buzzers (3.3V / 5V)** | 2 to 3 | Audible advance warning at intersections |
| **Controls & Power**| **Tactile Push Buttons (6x6mm)** | 4 | On ambulance beacon (simulate progress & route change) |
| | **Half-Size Solderless Breadboards**| 4 | 1 per junction + 1 for ambulance |
| | **Jumper Wires (M-M, M-F)** | ~60 | Wiring breadboards |
| | **USB Power Bank (5V)** | 1 | Portable power for the toy ambulance |
| **Props** | **Toy Cars & Ambulance (Hot Wheels)**| 8–10 | Queued vehicles and emergency vehicle |
| | **Road Mat / Foam Board** | 1 | 3-intersection corridor (~60 × 40 cm) |

> 💡 **Pro-Tip**: If available, use pre-assembled **Traffic Light LED Modules** (the little 3-pin Red/Yellow/Green sticks with built-in resistors). They save 24 loose resistors and make breadboard wiring clean and sturdy.

---

## ⚡ Hardware Pinout & Wiring

> ⚠️ **CRITICAL VOLTAGE RULE**: Both the Arduino UNO Q headers and the ESP32 GPIOs operate at **3.3V logic**. Never feed 5V signals directly into GPIO inputs. Always put a **330Ω resistor** in series with each LED anode (+ long leg).

### Junction 1 — Arduino UNO Q (Top Header Pins)
The STM32U585 MCU directly controls these pins:

| Approach | Red LED | Yellow LED | Green LED |
|---|:---:|:---:|:---:|
| **North** | `D2` | `D3` | `D4` |
| **South** | `D5` | `D6` | `D7` |
| **East**  | `D8` | `D9` | `D10` |
| **West**  | `D11` | `D12` | `D13` |
- **Cathodes (-)**: All connect to the `GND` rail.
- **USB Webcam**: Plug into the UNO Q USB-C port via a standard USB-A to USB-C OTG adapter.

### Junctions 2 & 3 — ESP32 DevKit v1
*(Identical wiring layout for both junctions)*

| Approach | Red LED | Yellow LED | Green LED |
|---|:---:|:---:|:---:|
| **North** | `GPIO13` | `GPIO12` | `GPIO14` |
| **South** | `GPIO27` | `GPIO26` | `GPIO25` |
| **East**  | `GPIO33` | `GPIO32` | `GPIO23` |
| **West**  | `GPIO22` | `GPIO21` | `GPIO19` |

- **East IR Sensor**: `GPIO36` (VP - Input only)
- **West IR Sensor**: `GPIO39` (VN - Input only)
- **Warning Buzzer**: `GPIO18` (Positive pin to GPIO18, negative to GND)

### Ambulance Position Beacon — ESP32 DevKit v1
Buttons connect between the designated GPIO pin and **GND** (utilizing internal `INPUT_PULLUP`):

| Input | Pin | Action Triggered |
|---|:---:|---|
| **Button 1** | `GPIO15` | Position: *"Approaching Junction 1"* |
| **Button 2** | `GPIO4`  | Position: *"Approaching Junction 2"* |
| **Button 3** | `GPIO16` | Position: *"Approaching Junction 3"* |
| **Button 4** | `GPIO17` | Route complete / simulate route diversion |
| **Status LED** | `GPIO2` (Built-in) | Blinks on each MQTT position broadcast |

---

##  Step-by-Step Recreation Guide

```
flowsync/
├── firmware/
│   ├── j1_mcu_safety/j1_mcu_safety.ino      # STM32 safety sketch
│   ├── junction_esp32/junction_esp32.ino    # ESP32 junction sketch
│   └── ambulance_beacon/ambulance_beacon.ino# ESP32 ambulance beacon
├── backend/
│   ├── server.py                            # Flask server (Public + Operator)
│   ├── core.py                              # Pre-clearing & what-if engine
│   ├── llm_tools.py                         # Tool schemas & Gemini/Mock LLM
│   ├── camera_queue.py                      # OpenCV queue tracker for J1
│   ├── setup_uno_q.sh                       # One-click Linux setup
│   └── templates/                           # Dark-theme HTML dashboards
└── sim/
    ├── build_network.bat                    # Network compiler
    ├── corridor.nod.xml / edg.xml / rou.xml # SUMO network files
    ├── run_comparison.py                    # 20-run benchmark script
    └── plot_results.py                      # Chart generator
```

### 1. Flash the Microcontrollers
In the **Arduino IDE**:
1. **Junction 1**: Select board `Arduino UNO Q (STM32 Core)`. Open `firmware/j1_mcu_safety/j1_mcu_safety.ino` and upload.
2. **Junction 2**: Select board `ESP32 Dev Module`. Open `firmware/junction_esp32/junction_esp32.ino`. Make sure line 37 says `#define JUNCTION_ID "J2"` and upload.
3. **Junction 3**: In the same file, change line 37 to `#define JUNCTION_ID "J3"` and upload to the second ESP32.
4. **Ambulance**: Open `firmware/ambulance_beacon/ambulance_beacon.ino` and upload to the third ESP32.

> **Self-Test on Boot**: Every board runs an automatic self-test on startup, cycling all red, yellow, and green LEDs sequentially for 1 second so you can instantly catch wiring errors.

---

### 2. Start the UNO Q Backend (or Run Locally on Laptop)
To run on the UNO Q Linux environment (or any Linux/macOS/WSL machine):

```bash
cd flowsync/backend
chmod +x setup_uno_q.sh
./setup_uno_q.sh
```

This installs `mosquitto`, creates a Python virtual environment, installs requirements, and starts the MQTT broker.

Then start the server:
```bash
source venv/bin/activate
python server.py
```

Optional: To enable live queue tracking via USB camera at Junction 1:
```bash
python camera_queue.py
```
*(If no camera is connected, `camera_queue.py` automatically falls back to an intelligent mock feed).*

Access the dashboards:
- **Public Traffic Advisory**: `http://localhost:5000/`
- **Operator Command Console**: `http://localhost:5000/login`
  - *Default Credentials*: `operator` / `flowsync2026`

---

### 3. Run the SUMO Simulation Benchmark
To generate macroscopic statistical validation comparing FlowSync against city baselines:

1. Install **SUMO** (Simulation of Urban Mobility):
   ```bash
   winget install Eclipse.SUMO
   pip install traci sumolib matplotlib numpy
   ```
2. Navigate to `flowsync/sim/` and compile the network:
   ```bash
   build_network.bat
   ```
3. Run the automated 4-scenario benchmark (5 runs each = 20 iterations):
   ```bash
   python run_comparison.py
   ```
4. Render presentation-ready comparison graphics:
   ```bash
   python plot_results.py
   ```
   Outputs 4 high-res charts in `flowsync/sim/results/` (`01_ev_travel_time.png`, `02_traffic_delay.png`, `03_queue_dynamics_j2.png`, `04_combined_tradeoff.png`).

---

## Built vs. Simulated Boundary

To maintain complete scientific and academic honesty during hackathon demonstrations, the system's capabilities are explicitly demarcated:

| Feature | Physically Built (On Table) | Macroscopically Simulated (In SUMO) |
|---|:---:|:---:|
| **Signal Phase Timing & Cycles** |  Real STM32 & ESP32 LEDs | |
| **Deterministic Safety (Min Green / All-Red)**| Enforced in MCU C++ firmware | |
| **Watchdog Reversion to Fixed Cycles** |  Tested via sensor disconnect | |
| **Queue Detection** |  Real IR sensors + USB Webcam | |
| **Mesh Networking & Messaging** |  Mosquitto MQTT over local Wi-Fi | |
| **Role-Based Web Dashboards** |  Live Flask UI with SSE streams | |
| **LLM Reasoning & Tool Orchestration** |  Gemini 2.0 API + `what_if` tool | |
| **Privacy Token Rotation** |  30s rotating tokens on ESP32 | |
| **Vehicle Trajectories** | Button-driven position beacons | High-speed continuous GPS kinematics |
| **Corridor Scale** | 3 tabletop intersections | Multi-kilometer 2-lane dual carriageway |
| **Statistical Generalization** | 5 physical test runs | 20 runs across randomized random traffic seeds |

---

##  The 5-Minute Stage Demo Walkthrough

| Time | Action | What to Point Out |
|---|---|---|
| **0:00 - 1:00** | System starts in normal adaptive cycle. | LEDs cycle smoothly across all 3 junctions. Dashboard shows live queues and sensor health. Highlight that the MCU controls the lights, not the cloud. |
| **1:00 - 1:45** | Place a stalled toy car on J1 approach. | Camera/IR detects the queue. On Operator UI, ask the AI: *"Incident at J1"*. The LLM calls `what_if` and `suggest_alt_route` to recommend an alternate route with tool-computed numbers. |
| **1:45 - 2:45** | Place 3 queued cars at J2. Press **Button 1** on Ambulance. | Public dashboard issues advance warning: *"Ambulance approaching from west (~15s). Please make way when safe."* **Watch Junction 2**: it starts flushing the exit queue *before* the ambulance gets there. Buzzer beeps. |
| **2:45 - 3:30** | Press **Button 4** (Route diversion). | Warnings immediately redirect away from cancelled junctions. Then release all buttons for >3s: warnings expire as *"EV location data stale"*, preventing phantom alarms. |
| **3:30 - 4:15** | Disconnect an IR sensor at J2. | Watchdog triggers. Mode flips to `⚠ Fixed-Time Fallback` with an instant audit log entry. Safety is never compromised by sensing faults. |
| **4:15 - 5:00** | Present SUMO Simulation Chart (`04_combined_tradeoff.png`). | Show data: Plain preemption cut EV travel time by 34% but spiked cross-traffic delay by 62%. FlowSync Pre-Clearing cut EV travel time by **49%** while keeping cross-traffic delay near normal levels. |

---

##  Safety & Privacy Compliance Checklist

- [x] **No Unlawful Driving Directives**: System warnings strictly advise drivers to *"make way when safe and lawful"*. It never instructs vehicles to run red lights or violate traffic rules.
- [x] **"Don't Follow the Ambulance"**: Driver advisories explicitly include anti-tailgating reminders based on empirical collision studies.
- [x] **Driver Privacy by Design**: Public dashboards show only approach-level advisories (*"Westbound, ~15s"*). The ambulance uses rotating anonymous tokens (`ev_XXXXXX`), preventing persistent public surveillance.
- [x] **Fail-Safe Watchdog**: If MQTT or Linux communication fails for >5 seconds, junctions independently revert to standard fixed-time operation without conflicting green lights.

---

##  License & Credits

Developed for the **AI-Powered City Traffic Intelligence & Coordination (PS 3)** challenge. Released under the [MIT License](LICENSE).
