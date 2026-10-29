#!/usr/bin/env python3
"""
FlowSync — Publication-Ready Benchmark Visualizer
=================================================
Reads simulation outputs from results/comparison.csv & results/step_metrics.csv
Generates high-resolution dark-themed figures matching the FlowSync Web Dashboard:
  - 01_ev_travel_time.png: Ambulance transit time with error bars
  - 02_traffic_delay.png: Non-emergency vehicle delay impact
  - 03_queue_dynamics_j2.png: Physical queue evolution at Junction 2
  - 04_combined_tradeoff.png: Dual-axis corridor efficiency comparison

Usage:
  python plot_results.py          # Saves figures to results/
  python plot_results.py --show   # Saves and displays interactively
"""

import os
import sys
import csv

try:
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("[ERROR] matplotlib and numpy are required to run this script.")
    print("Install with: pip install matplotlib numpy")
    sys.exit(1)

RESULTS_DIR = "results"
COMP_CSV = os.path.join(RESULTS_DIR, "comparison.csv")
STEP_CSV = os.path.join(RESULTS_DIR, "step_metrics.csv")

# ─── FlowSync Visual Theme ─────────────────────────────────────
THEME = {
    "bg_dark": "#0a0a0f",
    "card_dark": "#1a1a2e",
    "accent_teal": "#00d4aa",
    "amber_warn": "#ffb300",
    "red_alert": "#ff4444",
    "text_light": "#e8e8f0",
    "text_muted": "#8888aa",
    "grid_color": "#2a2a4e",
}

def apply_dark_theme(fig, ax):
    fig.patch.set_facecolor(THEME["bg_dark"])
    ax.set_facecolor(THEME["card_dark"])
    ax.tick_params(colors=THEME["text_muted"], labelsize=10)
    ax.xaxis.label.set_color(THEME["text_light"])
    ax.yaxis.label.set_color(THEME["text_light"])
    ax.title.set_color(THEME["text_light"])
    for spine in ax.spines.values():
        spine.set_color(THEME["grid_color"])
    ax.grid(color=THEME["grid_color"], linestyle="--", linewidth=0.7, alpha=0.6)


def load_data():
    if not os.path.exists(COMP_CSV):
        print(f"[ERROR] '{COMP_CSV}' not found. Run 'python run_comparison.py' first.")
        sys.exit(1)

    records = []
    with open(COMP_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            records.append({
                "name": r["scenario_name"],
                "key": r["scenario_key"],
                "ev_mean": float(r["ev_time_mean"]),
                "ev_std": float(r["ev_time_std"]),
                "delay_mean": float(r["delay_mean"]),
                "delay_std": float(r["delay_std"]),
                "queue": float(r["avg_queue"])
            })
    return records


def plot_ev_times(records, show=False):
    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    apply_dark_theme(fig, ax)

    names = [r["name"] for r in records]
    means = [r["ev_mean"] for r in records]
    stds = [r["ev_std"] for r in records]

    # Pre-clearing gets prominent teal highlight
    colors = [
        THEME["text_muted"],
        "#4a90e2",
        THEME["amber_warn"],
        THEME["accent_teal"]
    ]

    bars = ax.bar(names, means, yerr=stds, capsize=6, color=colors, edgecolor=THEME["grid_color"], width=0.55)

    # Data value labels on top of bars
    for bar in bars:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2., h + 1.2,
                f"{h:.1f}s", ha='center', va='bottom', color=THEME["text_light"], fontweight='bold', fontsize=10)

    ax.set_ylabel("Ambulance Corridor Travel Time (seconds)", fontsize=11, fontweight='bold')
    ax.set_title("Emergency Vehicle Transit Time Across Scenarios (Lower is Better)", fontsize=12, pad=14, fontweight='bold')
    plt.xticks(rotation=12, ha="right")
    plt.tight_layout()

    out = os.path.join(RESULTS_DIR, "01_ev_travel_time.png")
    plt.savefig(out, facecolor=fig.get_facecolor(), edgecolor='none')
    print(f"[✓] Saved: {out}")
    if show: plt.show()
    plt.close()


def plot_delay_impact(records, show=False):
    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    apply_dark_theme(fig, ax)

    names = [r["name"] for r in records]
    means = [r["delay_mean"] for r in records]
    stds = [r["delay_std"] for r in records]

    colors = [
        THEME["text_muted"],
        "#4a90e2",
        THEME["red_alert"],  # Plain preemption increases delay to others
        THEME["accent_teal"] # Preclearing maintains low delay
    ]

    bars = ax.bar(names, means, yerr=stds, capsize=6, color=colors, edgecolor=THEME["grid_color"], width=0.55)

    for bar in bars:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2., h + 0.3,
                f"{h:.1f}s", ha='center', va='bottom', color=THEME["text_light"], fontweight='bold', fontsize=10)

    ax.set_ylabel("Average Delay per Vehicle (seconds)", fontsize=11, fontweight='bold')
    ax.set_title("Collateral Delay Imposed on Non-Emergency Traffic (Lower is Better)", fontsize=12, pad=14, fontweight='bold')
    plt.xticks(rotation=12, ha="right")
    plt.tight_layout()

    out = os.path.join(RESULTS_DIR, "02_traffic_delay.png")
    plt.savefig(out, facecolor=fig.get_facecolor(), edgecolor='none')
    print(f"[✓] Saved: {out}")
    if show: plt.show()
    plt.close()


def plot_queue_dynamics(show=False):
    if not os.path.exists(STEP_CSV):
        return

    steps = []
    scenarios_data = {}

    with open(STEP_CSV, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        cols = headers[1:]
        for c in cols:
            scenarios_data[c] = []

        for row in reader:
            if not row or not row[0]: continue
            steps.append(int(row[0]))
            for idx, c in enumerate(cols):
                val = float(row[idx+1]) if (idx+1 < len(row) and row[idx+1]) else 0.0
                scenarios_data[c].append(val)

    fig, ax = plt.subplots(figsize=(10, 5), dpi=300)
    apply_dark_theme(fig, ax)

    color_map = {
        "1_fixed": (THEME["text_muted"], "Fixed Time"),
        "2_adaptive": ("#4a90e2", "Adaptive"),
        "3_plain_preempt": (THEME["amber_warn"], "Plain Preemption"),
        "4_preclear": (THEME["accent_teal"], "FlowSync Pre-Clearing")
    }

    # Window of interest around when EV passes Junction 2 (t=100s to 240s)
    start_t, end_t = 80, 240
    sub_steps = [s for s in steps if start_t <= s <= end_t]
    idx_start = steps.index(sub_steps[0])
    idx_end = steps.index(sub_steps[-1]) + 1

    for key, (color, label) in color_map.items():
        if key in scenarios_data:
            line_vals = scenarios_data[key][idx_start:idx_end]
            lw = 2.5 if "preclear" in key else 1.5
            ax.plot(sub_steps, line_vals, label=label, color=color, linewidth=lw)

    # Highlight ambulance arrival window
    ax.axvspan(120, 145, color="#ff4444", alpha=0.15, label="Ambulance Passage Window")

    ax.set_xlabel("Simulation Elapsed Time (seconds)", fontsize=11, fontweight='bold')
    ax.set_ylabel("Queued Vehicles at Junction 2 (count)", fontsize=11, fontweight='bold')
    ax.set_title("Physical Queue Dissipation at Critical Junction Ahead of Arrival", fontsize=12, pad=14, fontweight='bold')
    ax.legend(facecolor=THEME["card_dark"], edgecolor=THEME["grid_color"], labelcolor=THEME["text_light"], loc="upper left")
    plt.tight_layout()

    out = os.path.join(RESULTS_DIR, "03_queue_dynamics_j2.png")
    plt.savefig(out, facecolor=fig.get_facecolor(), edgecolor='none')
    print(f"[✓] Saved: {out}")
    if show: plt.show()
    plt.close()


def plot_combined_tradeoff(records, show=False):
    fig, ax1 = plt.subplots(figsize=(9, 5), dpi=300)
    apply_dark_theme(fig, ax1)

    names = [r["name"] for r in records]
    ev_times = [r["ev_mean"] for r in records]
    delays = [r["delay_mean"] for r in records]

    x = np.arange(len(names))
    width = 0.35

    rects1 = ax1.bar(x - width/2, ev_times, width, label='EV Travel Time (s)', color=THEME["accent_teal"], edgecolor=THEME["grid_color"])
    
    ax2 = ax1.twinx()
    ax2.set_facecolor('none')
    ax2.tick_params(colors=THEME["text_muted"], labelsize=10)
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_color(THEME["grid_color"])
    ax2.spines['left'].set_visible(False)
    ax2.spines['bottom'].set_visible(False)

    rects2 = ax2.bar(x + width/2, delays, width, label='Avg Cross Delay (s)', color=THEME["amber_warn"], edgecolor=THEME["grid_color"])

    ax1.set_ylabel("Ambulance Travel Time (s)", color=THEME["accent_teal"], fontsize=11, fontweight='bold')
    ax2.set_ylabel("Cross Traffic Delay (s)", color=THEME["amber_warn"], fontsize=11, fontweight='bold')
    ax1.set_title("FlowSync Co-Optimization: Emergency Speed vs Public Traffic Delay", fontsize=12, pad=16, fontweight='bold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(names, rotation=10, ha="right")

    # Combine legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right', facecolor=THEME["card_dark"], edgecolor=THEME["grid_color"], labelcolor=THEME["text_light"])

    plt.tight_layout()
    out = os.path.join(RESULTS_DIR, "04_combined_tradeoff.png")
    plt.savefig(out, facecolor=fig.get_facecolor(), edgecolor='none')
    print(f"[✓] Saved: {out}")
    if show: plt.show()
    plt.close()


def main():
    interactive = "--show" in sys.argv
    print("\nGenerating FlowSync Benchmark Figures...")
    records = load_data()
    plot_ev_times(records, show=interactive)
    plot_delay_impact(records, show=interactive)
    plot_queue_dynamics(show=interactive)
    plot_combined_tradeoff(records, show=interactive)
    print("\n[SUCCESS] All benchmark graphics generated in 'results/' directory.\n")


if __name__ == "__main__":
    main()
