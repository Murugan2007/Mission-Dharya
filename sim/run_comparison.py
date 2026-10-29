#!/usr/bin/env python3
"""
FlowSync — Professional Traffic Simulation Benchmark
=====================================================
Runs multi-scenario simulations using SUMO + TraCI:
  1. Fixed Timing (Baseline)
  2. Queue-Adaptive Control
  3. Plain EV Preemption
  4. Pre-Clearing Preemption (FlowSync Upgrade #1)

Compares:
  - Emergency vehicle travel time through the corridor
  - Average travel delay imposed on ordinary traffic
  - Cumulative queue accumulation at junctions

Outputs:
  - Formatted terminal report with mean +/- std-dev across runs
  - results/comparison.csv (ready for reporting / plotting)
  - results/step_metrics.csv (per-second queue dynamics)
"""

import os
import sys
import math
import csv
import statistics
from collections import defaultdict

# ─── Locate SUMO / TraCI ───────────────────────────────────────
if 'SUMO_HOME' in os.environ:
    tools_dir = os.path.join(os.environ['SUMO_HOME'], 'tools')
    if tools_dir not in sys.path:
        sys.path.append(tools_dir)
else:
    # Common default paths on Windows
    win_defaults = [
        r"C:\Program Files (x86)\Eclipse\Sumo\tools",
        r"C:\Program Files\Eclipse\Sumo\tools",
    ]
    for p in win_defaults:
        if os.path.exists(p):
            sys.path.append(p)
            break

try:
    import traci
    import sumolib
except ImportError:
    print("[ERROR] Could not import 'traci' or 'sumolib'.")
    print("Please ensure SUMO is installed and SUMO_HOME environment variable is set.")
    print("Download from: https://eclipse.dev/sumo/")
    sys.exit(1)

# ─── Simulation Configuration ──────────────────────────────────
CONFIG_FILE = "corridor.sumocfg"
NET_FILE = "corridor.net.xml"
SIGNAL_IDS = ["J1", "J2", "J3"]
AMBULANCE_ID = "ambulance_01"

RUNS_PER_SCENARIO = 5
SIM_MAX_STEPS = 500  # seconds

PREEMPTION_RADIUS_M = 200.0
DISCHARGE_RATE_SEC_PER_VEH = 2.5
PRECLEAR_SAFETY_MARGIN_SEC = 4.0

RESULTS_DIR = "results"
os.makedirs(RESULTS_DIR, exist_ok=True)


# ─── Geometry & State Helpers ─────────────────────────────────
def calc_distance(pos_a, pos_b):
    return math.sqrt((pos_a[0] - pos_b[0])**2 + (pos_a[1] - pos_b[1])**2)


def get_controlled_incoming_lanes(tl_id):
    """Returns list of incoming lane IDs governed by traffic light tl_id."""
    links = traci.trafficlight.getControlledLinks(tl_id)
    lanes = set()
    for link_group in links:
        for link in link_group:
            if link and len(link) > 0:
                lanes.add(link[0])
    return list(lanes)


def get_ev_eta_to_junction(ev_id, junction_id):
    """Estimates seconds remaining until emergency vehicle arrives at junction."""
    if ev_id not in traci.vehicle.getIDList():
        return float('inf')
    ev_pos = traci.vehicle.getPosition(ev_id)
    junc_pos = traci.junction.getPosition(junction_id)
    
    # We are traveling West -> East. If vehicle is already past the junction, ETA is 0
    if ev_pos[0] >= junc_pos[0]:
        return 0.0

    dist = calc_distance(ev_pos, junc_pos)
    speed = max(traci.vehicle.getSpeed(ev_id), 1.5)  # avoid div by zero
    return dist / speed


# ─── Controller Implementations ───────────────────────────────

class BaseController:
    def __init__(self, name):
        self.name = name

    def on_start(self):
        pass

    def on_step(self, step):
        pass

    def on_finish(self):
        pass


class FixedTimeController(BaseController):
    """Leaves SUMO's predefined fixed-time cycles untouched."""
    def __init__(self):
        super().__init__("Fixed Time")


class AdaptiveController(BaseController):
    """
    Actuated queue-proportional timing.
    Extends green phase if queued vehicles are waiting on green lanes,
    up to a strict upper cap of 60s to prevent starvation.
    """
    def __init__(self):
        super().__init__("Adaptive (Queue-Aware)")
        self.lock_junctions = set()

    def on_step(self, step):
        for tl_id in SIGNAL_IDS:
            if tl_id in self.lock_junctions:
                continue

            state = traci.trafficlight.getRedYellowGreenState(tl_id)
            # Only extend during green phases (contains 'G' or 'g')
            if 'G' in state or 'g' in state:
                lanes = get_controlled_incoming_lanes(tl_id)
                # Count halting vehicles on lanes that currently have green
                halting_on_green = 0
                for idx, lane in enumerate(lanes):
                    if idx < len(state) and state[idx] in ('G', 'g'):
                        halting_on_green += traci.lane.getLastStepHaltingNumber(lane)

                # If queue backlog exists, extend by 3 seconds
                if halting_on_green >= 2:
                    remaining = traci.trafficlight.getNextSwitch(tl_id) - traci.simulation.getTime()
                    if remaining < 4.0:
                        traci.trafficlight.setPhaseDuration(tl_id, min(remaining + 4.0, 50.0))


class PlainPreemptionController(BaseController):
    """
    Standard emergency preemption:
    When EV is detected within PREEMPTION_RADIUS_M, immediately force
    the corridor phase to green (Phase 0). Holds until EV clears.
    """
    def __init__(self):
        super().__init__("Plain Preemption")
        self.preempted_signals = set()

    def on_step(self, step):
        active_vehicles = traci.vehicle.getIDList()
        if AMBULANCE_ID not in active_vehicles:
            self._release_all()
            return

        ev_pos = traci.vehicle.getPosition(AMBULANCE_ID)

        for tl_id in SIGNAL_IDS:
            junc_pos = traci.junction.getPosition(tl_id)
            dist = calc_distance(ev_pos, junc_pos)
            is_ahead = ev_pos[0] < (junc_pos[0] + 15.0)

            if is_ahead and dist <= PREEMPTION_RADIUS_M:
                if tl_id not in self.preempted_signals:
                    # Phase 0 corresponds to East-West through corridor green
                    traci.trafficlight.setPhase(tl_id, 0)
                    traci.trafficlight.setPhaseDuration(tl_id, 25.0)
                    self.preempted_signals.add(tl_id)
            elif not is_ahead or dist > (PREEMPTION_RADIUS_M * 1.4):
                if tl_id in self.preempted_signals:
                    self._release_signal(tl_id)

    def _release_signal(self, tl_id):
        traci.trafficlight.setProgram(tl_id, "0")
        self.preempted_signals.discard(tl_id)

    def _release_all(self):
        for tl_id in list(self.preempted_signals):
            self._release_signal(tl_id)

    def on_finish(self):
        self._release_all()


class PreClearingController(BaseController):
    """
    FlowSync Upgrade #1: Pre-Clearing Preemption.
    Instead of waiting for the EV to get close, calculates downstream
    queue discharge time. Initiates green extension in advance so the
    exit approach queue has completely dissipated right as the EV arrives.
    """
    def __init__(self):
        super().__init__("Pre-Clearing Preemption")
        self.state_map = {}  # tl_id -> state: 'normal'|'preclearing'|'preempted'
        for tl_id in SIGNAL_IDS:
            self.state_map[tl_id] = "normal"

    def on_step(self, step):
        active_vehicles = traci.vehicle.getIDList()
        if AMBULANCE_ID not in active_vehicles:
            self._reset_all()
            return

        ev_pos = traci.vehicle.getPosition(AMBULANCE_ID)

        for tl_id in SIGNAL_IDS:
            junc_pos = traci.junction.getPosition(tl_id)
            is_past = ev_pos[0] >= (junc_pos[0] + 25.0)

            if is_past:
                if self.state_map[tl_id] != "normal":
                    traci.trafficlight.setProgram(tl_id, "0")
                    self.state_map[tl_id] = "normal"
                continue

            eta = get_ev_eta_to_junction(AMBULANCE_ID, tl_id)

            # Query queued cars in the corridor direction (West->East approach)
            incoming_lanes = [l for l in get_controlled_incoming_lanes(tl_id) if "W_" in l or "_J" in l]
            halting_count = sum(traci.lane.getLastStepHaltingNumber(l) for l in incoming_lanes)
            clearance_needed = (halting_count * DISCHARGE_RATE_SEC_PER_VEH) + PRECLEAR_SAFETY_MARGIN_SEC

            # Phase 1: Pre-clear queue if ETA matches required discharge window
            if self.state_map[tl_id] == "normal" and eta <= clearance_needed and eta > 12.0:
                traci.trafficlight.setPhase(tl_id, 0)
                traci.trafficlight.setPhaseDuration(tl_id, max(clearance_needed + 5.0, 15.0))
                self.state_map[tl_id] = "preclearing"

            # Phase 2: Full corridor lock when EV enters critical approach window
            elif eta <= 12.0 and self.state_map[tl_id] in ("normal", "preclearing"):
                traci.trafficlight.setPhase(tl_id, 0)
                traci.trafficlight.setPhaseDuration(tl_id, 20.0)
                self.state_map[tl_id] = "preempted"

    def _reset_all(self):
        for tl_id in SIGNAL_IDS:
            if self.state_map.get(tl_id) != "normal":
                traci.trafficlight.setProgram(tl_id, "0")
                self.state_map[tl_id] = "normal"

    def on_finish(self):
        self._reset_all()


# ─── Metrics Collection ────────────────────────────────────────

class MetricsTracker:
    def __init__(self):
        self.ev_depart_time = None
        self.ev_arrival_time = None
        self.per_step_delays = []
        self.per_step_waiting = []
        self.per_step_queues = {tl: [] for tl in SIGNAL_IDS}

    def record_step(self, step):
        active_vehicles = traci.vehicle.getIDList()

        # Track Emergency Vehicle trip
        if AMBULANCE_ID in active_vehicles:
            if self.ev_depart_time is None:
                self.ev_depart_time = step
        elif self.ev_depart_time is not None and self.ev_arrival_time is None:
            self.ev_arrival_time = step

        # Non-EV vehicle delays and wait times
        non_evs = [v for v in active_vehicles if v != AMBULANCE_ID]
        if non_evs:
            avg_loss = sum(traci.vehicle.getTimeLoss(v) for v in non_evs) / len(non_evs)
            avg_wait = sum(traci.vehicle.getWaitingTime(v) for v in non_evs) / len(non_evs)
            self.per_step_delays.append(avg_loss)
            self.per_step_waiting.append(avg_wait)

        # Track physical queue length at each junction
        for tl_id in SIGNAL_IDS:
            lanes = get_controlled_incoming_lanes(tl_id)
            queue_len = sum(traci.lane.getLastStepHaltingNumber(l) for l in lanes)
            self.per_step_queues[tl_id].append(queue_len)

    def compute_summary(self):
        ev_duration = None
        if self.ev_depart_time is not None and self.ev_arrival_time is not None:
            ev_duration = self.ev_arrival_time - self.ev_depart_time

        avg_delay = statistics.mean(self.per_step_delays) if self.per_step_delays else 0.0
        avg_wait = statistics.mean(self.per_step_waiting) if self.per_step_waiting else 0.0
        
        avg_queues = {}
        for tl_id, q_history in self.per_step_queues.items():
            avg_queues[tl_id] = statistics.mean(q_history) if q_history else 0.0

        return {
            "ev_travel_time": ev_duration,
            "avg_delay": avg_delay,
            "avg_waiting": avg_wait,
            "avg_queue_total": sum(avg_queues.values()),
            "junction_queues": avg_queues,
            "step_queues_j2": self.per_step_queues["J2"]  # for time-series plotting
        }


# ─── Execution Engine ──────────────────────────────────────────

def run_single_simulation(controller, seed=42):
    sumo_binary = sumolib.checkBinary("sumo")
    sumo_cmd = [
        sumo_binary,
        "-c", CONFIG_FILE,
        "--seed", str(seed),
        "--no-step-log", "true",
        "--duration-log.disable", "true",
        "--lateral-resolution", "3.2"
    ]

    traci.start(sumo_cmd)
    controller.on_start()
    tracker = MetricsTracker()

    step = 0
    while step < SIM_MAX_STEPS and traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        controller.on_step(step)
        tracker.record_step(step)
        step += 1

    controller.on_finish()
    traci.close()
    return tracker.compute_summary()


def run_benchmark():
    # Make sure network file exists
    if not os.path.exists(NET_FILE):
        print(f"[!] Network file '{NET_FILE}' not found. Compiling corridor network...")
        cmd = f"netconvert --node-files=corridor.nod.xml --edge-files=corridor.edg.xml --output-file={NET_FILE} --tls.guess true"
        ret = os.system(cmd)
        if ret != 0:
            print("[ERROR] netconvert failed. Run build_network.bat manually.")
            sys.exit(1)

    scenarios = [
        ("1_fixed", FixedTimeController),
        ("2_adaptive", AdaptiveController),
        ("3_plain_preempt", PlainPreemptionController),
        ("4_preclear", PreClearingController),
    ]

    print("\n" + "=" * 70)
    print("   FLOWSYNC — URBAN MOBILITY SIMULATION (SUMO + TRACI)")
    print("   Validating Emergency Pre-Clearing vs Baselines across 5 runs")
    print("=" * 70)

    summary_records = []
    j2_time_series_by_scenario = {}

    for tag, ctrl_factory in scenarios:
        ctrl_instance = ctrl_factory()
        print(f"\n▶ Evaluating: {ctrl_instance.name} ({RUNS_PER_SCENARIO} runs)")

        ev_times = []
        delays = []
        waits = []
        queues = []
        median_j2_series = None

        for run_idx in range(RUNS_PER_SCENARIO):
            seed = 100 + (run_idx * 17)
            metrics = run_single_simulation(ctrl_factory(), seed=seed)

            if metrics["ev_travel_time"] is not None:
                ev_times.append(metrics["ev_travel_time"])
            delays.append(metrics["avg_delay"])
            waits.append(metrics["avg_waiting"])
            queues.append(metrics["avg_queue_total"])

            if run_idx == 0:
                median_j2_series = metrics["step_queues_j2"]

            ev_str = f"{metrics['ev_travel_time']:.1f}s" if metrics["ev_travel_time"] else "N/A"
            print(f"  • Run #{run_idx+1}: EV Time = {ev_str:>6} | Delay = {metrics['avg_delay']:>4.1f}s | Queue = {metrics['avg_queue_total']:>4.1f}")

        # Compute aggregate statistics
        ev_mean = statistics.mean(ev_times) if ev_times else 0.0
        ev_std = statistics.stdev(ev_times) if len(ev_times) > 1 else 0.0
        delay_mean = statistics.mean(delays)
        delay_std = statistics.stdev(delays) if len(delays) > 1 else 0.0
        queue_mean = statistics.mean(queues)

        summary_records.append({
            "scenario_key": tag,
            "scenario_name": ctrl_instance.name,
            "ev_time_mean": round(ev_mean, 2),
            "ev_time_std": round(ev_std, 2),
            "delay_mean": round(delay_mean, 2),
            "delay_std": round(delay_std, 2),
            "avg_queue": round(queue_mean, 2)
        })
        j2_time_series_by_scenario[tag] = median_j2_series

    # ─── Terminal Presentation Table ─────────────────────────────
    print("\n" + "=" * 70)
    print(f"{'Scenario':<26} {'Ambulance Time (s)':^20} {'Average Delay (s)':^20}")
    print("-" * 70)
    for rec in summary_records:
        ev_fmt = f"{rec['ev_time_mean']:.1f} ± {rec['ev_time_std']:.1f}"
        delay_fmt = f"{rec['delay_mean']:.1f} ± {rec['delay_std']:.1f}"
        print(f"{rec['scenario_name']:<26} {ev_fmt:^20} {delay_fmt:^20}")
    print("=" * 70)

    # ─── Export to CSV ───────────────────────────────────────────
    comp_csv_path = os.path.join(RESULTS_DIR, "comparison.csv")
    with open(comp_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "scenario_key", "scenario_name", "ev_time_mean", "ev_time_std",
            "delay_mean", "delay_std", "avg_queue"
        ])
        writer.writeheader()
        writer.writerows(summary_records)
    print(f"\n[✓] Primary benchmark written to: {comp_csv_path}")

    # Export J2 time-series for queue plot
    step_csv_path = os.path.join(RESULTS_DIR, "step_metrics.csv")
    with open(step_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        headers = ["step"] + [s[0] for s in scenarios]
        writer.writerow(headers)
        max_len = max(len(s) for s in j2_time_series_by_scenario.values())
        for step_i in range(max_len):
            row = [step_i]
            for tag, _ in scenarios:
                series = j2_time_series_by_scenario.get(tag, [])
                val = series[step_i] if step_i < len(series) else ""
                row.append(val)
            writer.writerow(row)
    print(f"[✓] Junction queue dynamics written to: {step_csv_path}")
    print("[✓] Ready for visualization: run 'python plot_results.py'\n")


if __name__ == "__main__":
    run_benchmark()
