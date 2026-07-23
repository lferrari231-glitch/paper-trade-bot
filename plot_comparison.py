#!/usr/bin/env python3
"""
======================================================================
 plot_comparison.py
======================================================================
Confronto diretto tra:
  - Bot base (paper_log.csv / paper_state.json) - senza leva
  - Bot a leva (paper_log_leverage.csv / paper_state_leverage.json) - 2x + stop -5%

Genera un unico grafico con le due equity curve sovrapposte, per vedere
a colpo d'occhio se la leva+stop sta aiutando o penalizzando rispetto
alla versione semplice.

Legge:  paper_log.csv, paper_log_leverage.csv (se presente)
Scrive: comparison_chart.png

USO:
  python3 plot_comparison.py
"""

import sys
import os
import json
import csv
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_log(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["timestamp"] = parse_ts(r["timestamp"])
            r["mtm_capital"] = float(r["mtm_capital"])
            rows.append(r)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def main():
    base_log = os.path.join(SCRIPT_DIR, "paper_log.csv")
    lev_log = os.path.join(SCRIPT_DIR, "paper_log_leverage.csv")

    base_rows = load_log(base_log)
    lev_rows = load_log(lev_log)

    if not base_rows and not lev_rows:
        print("[ERROR] Nessun log trovato (paper_log.csv / paper_log_leverage.csv).")
        sys.exit(1)

    fig, ax = plt.subplots(figsize=(11, 6))

    if base_rows:
        times = [r["timestamp"] for r in base_rows]
        caps = [r["mtm_capital"] for r in base_rows]
        ax.plot(times, caps, color="#2563eb", linewidth=1.8, marker="o",
                markersize=3, label="Base (senza leva)")
        last_base = caps[-1]
    else:
        last_base = None

    if lev_rows:
        times_l = [r["timestamp"] for r in lev_rows]
        caps_l = [r["mtm_capital"] for r in lev_rows]
        ax.plot(times_l, caps_l, color="#f59e0b", linewidth=1.8, marker="o",
                markersize=3, label="Leva 2x + stop -5%")
        # marker sui trigger di stop-loss
        stop_rows = [r for r in lev_rows if r["event"] == "stop_loss"]
        if stop_rows:
            ax.scatter([r["timestamp"] for r in stop_rows],
                       [r["mtm_capital"] for r in stop_rows],
                       color="#dc2626", marker="x", s=90, zorder=5, label="Stop-loss scattato")
        last_lev = caps_l[-1]
    else:
        last_lev = None

    initial_capital = None
    if os.path.exists(os.path.join(SCRIPT_DIR, "paper_state.json")):
        with open(os.path.join(SCRIPT_DIR, "paper_state.json")) as f:
            initial_capital = json.load(f).get("initial_capital")
    if initial_capital:
        ax.axhline(initial_capital, color="#9ca3af", linewidth=1, linestyle=":",
                   label=f"Capitale iniziale (\\${initial_capital:,.0f})")

    ax.set_title("Confronto: Paper Trading base vs a leva (2x, stop -5%)", fontsize=13, fontweight="bold")
    ax.set_ylabel("Capitale virtuale ($)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=9, framealpha=0.9)

    footer = []
    if last_base is not None and initial_capital:
        footer.append(f"Base: \\${last_base:,.2f} ({(last_base/initial_capital-1)*100:+.2f}%)")
    if last_lev is not None and initial_capital:
        footer.append(f"Leva: \\${last_lev:,.2f} ({(last_lev/initial_capital-1)*100:+.2f}%)")
    fig.text(0.01, 0.01, "  |  ".join(footer), fontsize=9, color="#6b7280")

    fig.tight_layout(rect=[0, 0.03, 1, 1])
    outfile = os.path.join(SCRIPT_DIR, "comparison_chart.png")
    fig.savefig(outfile, dpi=150)
    print(f"[OK] Grafico di confronto salvato in {outfile}")


if __name__ == "__main__":
    main()
