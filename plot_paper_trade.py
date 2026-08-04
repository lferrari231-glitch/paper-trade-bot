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
  - Se disponibile, overlay tratteggiato della posizione a leva (2x + stop
    -5%, vedi paper_trade_leverage.py) per confronto diretto con la base

Legge:  paper_log.csv, paper_state.json  (stessa cartella dello script)
        paper_log_leverage.csv, paper_state_leverage.json (opzionali)
Scrive: paper_trade_chart.png

USO:
  python3 plot_paper_trade.py
  python3 plot_paper_trade.py --outfile chart.png
"""

import sys
import os
import json
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "paper_log.csv")
STATE_FILE = os.path.join(SCRIPT_DIR, "paper_state.json")
LOG_FILE_LEV = os.path.join(SCRIPT_DIR, "paper_log_leverage.csv")
STATE_FILE_LEV = os.path.join(SCRIPT_DIR, "paper_state_leverage.json")
REBALANCE_DAYS = 7
# Finestra temporale mostrata nel grafico: evita che lunghi tratti piatti
# (nessuna posizione aperta per settimane) schiaccino la curva vera in un
# angolo. Se la storia e' piu' corta della finestra, si mostra tutto.
WINDOW_DAYS = 35
# Parametri della versione a leva (paper_trade_leverage.py). Non sono salvati
# nello state file, quindi li teniamo qui come default per l'etichetta del grafico.
LEV_DEFAULT_X = 2.0
LEV_DEFAULT_STOP = 0.05


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

    # --- Prossimo ribilanciamento (serve gia' qui per definire la finestra) ---
    last_rebal_iso = state.get("last_rebalance")
    next_rebal = None
    if last_rebal_iso:
        next_rebal = parse_ts(last_rebal_iso) + timedelta(days=REBALANCE_DAYS)

    # --- Finestra temporale: ultimi WINDOW_DAYS, per non schiacciare la
    # curva vera dietro a lunghi tratti piatti di storia lontana ---
    full_start, full_end = times[0], times[-1]
    window_start = full_end - timedelta(days=WINDOW_DAYS)
    view_start = max(full_start, window_start)
    plot_rows = [r for r in rows if r["timestamp"] >= view_start]
    # includi sempre il punto immediatamente precedente alla finestra, cosi'
    # la linea non parte "a mezz'aria"
    if view_start > full_start:
        idx = max(i for i, r in enumerate(rows) if r["timestamp"] < view_start)
        plot_rows = [rows[idx]] + plot_rows
    plot_times = [r["timestamp"] for r in plot_rows]
    plot_caps = [r["mtm_capital"] for r in plot_rows]

    # --- Posizione a leva (opzionale): stessa finestra temporale della base ---
    lev_rows_all = load_log(LOG_FILE_LEV) if os.path.exists(LOG_FILE_LEV) else []
    lev_state = load_state(STATE_FILE_LEV) if os.path.exists(STATE_FILE_LEV) else {}
    lev_plot_rows = [r for r in lev_rows_all if r["timestamp"] >= view_start]
    if lev_rows_all and view_start > lev_rows_all[0]["timestamp"]:
        idx_l = max((i for i, r in enumerate(lev_rows_all) if r["timestamp"] < view_start), default=None)
        if idx_l is not None:
            lev_plot_rows = [lev_rows_all[idx_l]] + lev_plot_rows
    lev_plot_times = [r["timestamp"] for r in lev_plot_rows]
    lev_plot_caps = [r["mtm_capital"] for r in lev_plot_rows]

    fig, ax = plt.subplots(figsize=(12, 6.5))

    # --- Equity curve ---
    ax.plot(plot_times, plot_caps, color="#2563eb", linewidth=2.0, marker="o",
            markersize=4, label="Capitale (mark-to-market)", zorder=4)
    ax.axhline(initial_capital, color="#9ca3af", linewidth=1, linestyle=":",
               label=f"Capitale iniziale (${initial_capital:,.0f})", zorder=2)

    # --- Equity curve a leva (tratteggiata), se disponibile ---
    if lev_plot_caps:
        lev_x = lev_state.get("leverage", LEV_DEFAULT_X)
        lev_stop = lev_state.get("stop_loss_pct", LEV_DEFAULT_STOP)
        lev_label = f"Leva {lev_x:.0f}x" + (f" + stop -{lev_stop*100:.0f}%" if lev_stop else "")
        ax.plot(lev_plot_times, lev_plot_caps, color="#f59e0b", linewidth=1.8,
                linestyle="--", marker="o", markersize=3, alpha=0.9,
                label=lev_label, zorder=3)
        stop_rows = [r for r in lev_plot_rows if r["event"] == "stop_loss"]
        if stop_rows:
            ax.scatter([r["timestamp"] for r in stop_rows],
                       [r["mtm_capital"] for r in stop_rows],
                       color="#dc2626", marker="x", s=90, linewidth=2, zorder=6,
                       label="Stop-loss (leva)")

    # --- Entry / exit / rotation markers ---
    # Piu' eventi (exit+enter, oppure exit+to_cash) possono condividere lo
    # stesso timestamp: li raggruppiamo per evitare etichette sovrapposte.
    by_ts = defaultdict(list)
    for r in plot_rows:
        if r["event"] in ("enter", "exit", "to_cash"):
            by_ts[r["timestamp"]].append(r)

    entry_plotted = exit_plotted = rot_plotted = False
    for ts, evs in sorted(by_ts.items()):
        cap = evs[0]["mtm_capital"]
        kinds = {e["event"] for e in evs}
        if "exit" in kinds and "enter" in kinds:
            frm = next(e["symbol"] for e in evs if e["event"] == "exit")
            to = next(e["symbol"] for e in evs if e["event"] == "enter")
            ax.scatter(ts, cap, color="#7c3aed", marker="D", s=90, zorder=6,
                       label="Rotazione" if not rot_plotted else None)
            ax.annotate(f"{frm}→{to}", (ts, cap), textcoords="offset points",
                        xytext=(0, 12), ha="center", fontsize=8, color="#7c3aed",
                        fontweight="bold")
            rot_plotted = True
        elif "exit" in kinds and "to_cash" in kinds:
            frm = next(e["symbol"] for e in evs if e["event"] == "exit")
            ax.scatter(ts, cap, color="#dc2626", marker="v", s=110, zorder=6,
                       label="Uscita" if not exit_plotted else None)
            ax.annotate(f"{frm}→CASH", (ts, cap), textcoords="offset points",
                        xytext=(0, -16), ha="center", fontsize=8, color="#dc2626",
                        fontweight="bold")
            exit_plotted = True
        elif "enter" in kinds:
            sym = evs[0]["symbol"]
            ax.scatter(ts, cap, color="#16a34a", marker="^", s=110, zorder=6,
                       label="Ingresso" if not entry_plotted else None)
            ax.annotate(sym, (ts, cap), textcoords="offset points",
                        xytext=(0, 12), ha="center", fontsize=8, color="#16a34a",
                        fontweight="bold")
            entry_plotted = True
        elif "exit" in kinds:
            sym = evs[0]["symbol"]
            ax.scatter(ts, cap, color="#dc2626", marker="v", s=110, zorder=6,
                       label="Uscita" if not exit_plotted else None)
            ax.annotate(sym, (ts, cap), textcoords="offset points",
                        xytext=(0, -16), ha="center", fontsize=8, color="#dc2626",
                        fontweight="bold")
            exit_plotted = True

    # --- Past rebalances (from state history), solo quelli nella finestra ---
    rebal_plotted = False
    for h in state.get("history", []):
        ts = parse_ts(h["timestamp"])
        if ts < view_start:
            continue
        ax.axvline(ts, color="#a855f7", linewidth=1, linestyle="--", alpha=0.5,
                   label="Ribilanciamento" if not rebal_plotted else None, zorder=1)
        rebal_plotted = True

    # --- Next scheduled rebalance ---
    if next_rebal:
        ax.axvline(next_rebal, color="#f59e0b", linewidth=1.5, linestyle="--",
                   label=f"Prossimo ribilanciamento ({next_rebal.strftime('%d/%m')})",
                   zorder=1)

    # --- Asse x: finestra fissa con margine, tick settimanali ---
    x_left = view_start - timedelta(hours=12)
    x_right = (next_rebal if next_rebal else plot_times[-1]) + timedelta(days=1.5)
    ax.set_xlim(x_left, x_right)
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=7))
    ax.xaxis.set_minor_locator(mdates.DayLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))

    # --- Asse y: padding cosi' la curva non tocca i bordi, formattazione $ ---
    y_values = plot_caps + [initial_capital] + lev_plot_caps
    y_min, y_max = min(y_values), max(y_values)
    y_range = max(y_max - y_min, 1)
    pad = y_range * 0.15
    ax.set_ylim(y_min - pad, y_max + pad)
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("${x:,.0f}"))
    ax.yaxis.set_minor_locator(mticker.AutoMinorLocator(2))

    # --- Formatting generale ---
    ax.set_title("Paper Trading — Naive Doppio Momentum (BTC/ETH/SOL/BNB)",
                 fontsize=14, fontweight="bold", pad=14)
    ax.set_ylabel("Capitale virtuale ($)", fontsize=10)
    fig.autofmt_xdate(rotation=30, ha="right")
    ax.grid(True, which="major", alpha=0.3)
    ax.grid(True, which="minor", alpha=0.1)
    ax.tick_params(axis="both", labelsize=9)
    ax.legend(loc="upper left", fontsize=8.5, framealpha=0.92, ncols=1)

    last_cap = caps[-1] if caps else initial_capital
    total_return = (last_cap / initial_capital - 1) * 100
    pos = state.get("current_symbol") or "CASH"
    window_note = (f" (ultimi {WINDOW_DAYS}g)" if view_start > full_start else "")
    footer = (f"Ultimo aggiornamento: {times[-1].strftime('%d/%m/%Y %H:%M UTC') if times else 'n/d'}"
              f"   |   Posizione: {pos}   |   Rendimento totale: {total_return:+.2f}%")
    if lev_plot_caps:
        lev_initial = lev_state.get("initial_capital", initial_capital)
        lev_return = (lev_plot_caps[-1] / lev_initial - 1) * 100
        lev_pos = lev_state.get("current_symbol") or "CASH"
        footer += f"   |   Leva: {lev_pos}, {lev_return:+.2f}%"
    footer += f"   |   Vista{window_note}"
    fig.text(0.01, 0.01, footer, fontsize=8.5, color="#6b7280")

    fig.tight_layout(rect=[0, 0.035, 1, 1])
    fig.savefig(outfile, dpi=150)
    print(f"[OK] Grafico salvato in {outfile}")


if __name__ == "__main__":
    main()
