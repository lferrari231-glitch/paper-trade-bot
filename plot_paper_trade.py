#!/usr/bin/env python3
"""
======================================================================
 plot_paper_trade.py
======================================================================
Genera un grafico giornaliero del paper trading "Naive Doppio Momentum":
  - Equity curve (capitale mark-to-market nel tempo)
  - Marker di ingresso (verde) e uscita (rosso) per ogni asset
  - Linee verticali sui ribilanciamenti passati (con etichetta asset)
  - Linea verticale tratteggiata sul prossimo ribilanciamento previsto

Legge:  paper_log.csv, paper_state.json  (stessa cartella dello script)
Scrive: paper_trade_chart.png

USO:
  python3 plot_paper_trade.py
  python3 plot_paper_trade.py --outfile chart.png
"""

import sys
import os
import json
import csv
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "paper_log.csv")
STATE_FILE = os.path.join(SCRIPT_DIR, "paper_state.json")
REBALANCE_DAYS = 7


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_log(path):
    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["timestamp"] = parse_ts(r["timestamp"])
            r["mtm_capital"] = float(r["mtm_capital"])
            rows.append(r)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def load_state(path):
    with open(path) as f:
        return json.load(f)


def main():
    args = sys.argv[1:]
    outfile = os.path.join(SCRIPT_DIR, "paper_trade_chart.png")
    if "--outfile" in args:
        i = args.index("--outfile")
        if i + 1 < len(args):
            outfile = args[i + 1]

    if not os.path.exists(LOG_FILE) or not os.path.exists(STATE_FILE):
        print("[ERROR] paper_log.csv o paper_state.json non trovati.")
        sys.exit(1)

    rows = load_log(LOG_FILE)
    state = load_state(STATE_FILE)

    times = [r["timestamp"] for r in rows]
    caps = [r["mtm_capital"] for r in rows]
    initial_capital = state.get("initial_capital", caps[0] if caps else 5000)

    fig, ax = plt.subplots(figsize=(11, 6))

    # --- Equity curve ---
    ax.plot(times, caps, color="#2563eb", linewidth=1.8, marker="o",
            markersize=3, label="Capitale (mark-to-market)")
    ax.axhline(initial_capital, color="#9ca3af", linewidth=1, linestyle=":",
               label=f"Capitale iniziale (${initial_capital:,.0f})")

    # --- Entry / exit markers ---
    entry_plotted = exit_plotted = False
    for r in rows:
        ev = r["event"]
        if ev == "enter":
            ax.scatter(r["timestamp"], r["mtm_capital"], color="#16a34a",
                       marker="^", s=110, zorder=5,
                       label="Ingresso" if not entry_plotted else None)
            ax.annotate(r["symbol"], (r["timestamp"], r["mtm_capital"]),
                        textcoords="offset points", xytext=(0, 10),
                        ha="center", fontsize=8, color="#16a34a", fontweight="bold")
            entry_plotted = True
        elif ev in ("exit", "to_cash"):
            ax.scatter(r["timestamp"], r["mtm_capital"], color="#dc2626",
                       marker="v", s=110, zorder=5,
                       label="Uscita" if not exit_plotted else None)
            label = r["symbol"] if ev == "exit" else "CASH"
            ax.annotate(label, (r["timestamp"], r["mtm_capital"]),
                        textcoords="offset points", xytext=(0, -14),
                        ha="center", fontsize=8, color="#dc2626", fontweight="bold")
            exit_plotted = True

    # --- Past rebalances (from state history) ---
    rebal_plotted = False
    for h in state.get("history", []):
        ts = parse_ts(h["timestamp"])
        ax.axvline(ts, color="#a855f7", linewidth=1, linestyle="--", alpha=0.6,
                   label="Ribilanciamento" if not rebal_plotted else None)
        rebal_plotted = True

    # --- Next scheduled rebalance ---
    last_rebal_iso = state.get("last_rebalance")
    if last_rebal_iso:
        last_rebal = parse_ts(last_rebal_iso)
        next_rebal = last_rebal + timedelta(days=REBALANCE_DAYS)
        ax.axvline(next_rebal, color="#f59e0b", linewidth=1.5, linestyle="--",
                   label=f"Prossimo ribilanciamento ({next_rebal.strftime('%d/%m')})")
        # estendi asse x per includere la data futura
        ax.set_xlim(right=next_rebal + timedelta(days=1))

    # --- Formatting ---
    ax.set_title("Paper Trading — Naive Doppio Momentum (BTC/ETH/SOL/BNB)", fontsize=13, fontweight="bold")
    ax.set_ylabel("Capitale virtuale ($)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8, framealpha=0.9)

    last_cap = caps[-1] if caps else initial_capital
    total_return = (last_cap / initial_capital - 1) * 100
    pos = state.get("current_symbol") or "CASH"
    fig.text(0.01, 0.01,
              f"Ultimo aggiornamento: {times[-1].strftime('%d/%m/%Y %H:%M UTC') if times else 'n/d'}  |  "
              f"Posizione: {pos}  |  Rendimento totale: {total_return:+.2f}%",
              fontsize=8, color="#6b7280")

    fig.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(outfile, dpi=150)
    print(f"[OK] Grafico salvato in {outfile}")


if __name__ == "__main__":
    main()
