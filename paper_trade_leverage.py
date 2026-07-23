#!/usr/bin/env python3
"""
======================================================================
 Paper Trading A LEVA - Naive Doppio Momentum (versione di confronto)
======================================================================
Gira in PARALLELO a paper_trade.py, usando lo stesso segnale e lo stesso
timing di rebalance settimanale, ma applicando:
  - Leva 2x (LEVERAGE)
  - Stop-loss -5% sul prezzo rispetto al prezzo di entrata (STOP_LOSS_PCT)

Stato e log separati dal bot base (non lo tocca):
  paper_state_leverage.json
  paper_log_leverage.csv

PRIMO AVVIO (nessun paper_state_leverage.json presente):
  Ricostruisce lo storico a partire dai rebalance gia' avvenuti nel bot
  base (legge paper_state.json), rigiocando le stesse finestre temporali
  con leva+stop sui prezzi storici giornalieri. Per il periodo storico si
  usa il minimo/massimo giornaliero per capire se lo stop sarebbe scattato
  quel giorno: e' un'approssimazione, perche' non abbiamo prezzi
  infragiornalieri piu' fini per il passato.

DA OGGI IN POI: pensato per girare ogni ~4 ore (vedi workflow GitHub
Actions dedicato), cosi' il controllo dello stop-loss e' molto piu'
preciso rispetto a un check giornaliero.

USO:
  python3 paper_trade_leverage.py
"""

import os
import sys
import json
from datetime import datetime, timezone

import paper_trade as base  # riusa fetch prezzi, segnale, telegram

# ======================================================================
# CONFIG
# ======================================================================
LEVERAGE       = 2.0
STOP_LOSS_PCT  = 0.05          # -5% sul prezzo rispetto all'entrata

FEE_PER_SIDE   = base.FEE_PER_SIDE * LEVERAGE   # fee sul nozionale (capitale x leva)
SLIPPAGE       = base.SLIPPAGE * LEVERAGE
REBALANCE_DAYS = base.REBALANCE_DAYS
SYMBOLS        = base.SYMBOLS

STATE_FILE          = "paper_state_leverage.json"
LOG_FILE            = "paper_log_leverage.csv"
BASELINE_STATE_FILE = "paper_state.json"


# ======================================================================
# STATE / LOG (separati dal bot base)
# ======================================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def init_state(capital):
    return {
        "initial_capital":  capital,
        "capital_at_entry": capital,
        "current_symbol":   None,
        "entry_price":      None,
        "entry_time":       None,
        "last_rebalance":   None,
        "trade_count":      0,
        "win_count":        0,
        "stop_count":       0,
        "history":          [],
    }


def append_log(timestamp, event, symbol, mtm_capital, current_prices, note=""):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a") as f:
        if is_new:
            f.write("timestamp,event,symbol,mtm_capital,btc,eth,sol,bnb,note\n")
        prices_str = ",".join(
            f"{current_prices.get(s, ''):.2f}" if current_prices.get(s) else ""
            for s in SYMBOLS
        )
        f.write(f"{timestamp},{event},{symbol or 'CASH'},"
                f"{mtm_capital:.2f},{prices_str},\"{note}\"\n")


def parse_iso(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ======================================================================
# BACKFILL: ricostruisce lo storico a leva dai rebalance gia' avvenuti
# ======================================================================
def backfill_from_baseline():
    if not os.path.exists(BASELINE_STATE_FILE):
        return None
    with open(BASELINE_STATE_FILE) as f:
        baseline = json.load(f)

    history = baseline.get("history", [])
    if not history:
        return None

    capital = baseline["initial_capital"]
    state = init_state(capital)
    now = datetime.now(timezone.utc)

    print(f"[BACKFILL] Ricostruzione storico a leva {LEVERAGE}x da {len(history)} rebalance del bot base...")

    for i, h in enumerate(history):
        symbol = h.get("to")
        start_ts = parse_iso(h["timestamp"])
        end_ts = parse_iso(history[i + 1]["timestamp"]) if i + 1 < len(history) else now

        if symbol is None:
            # periodo CASH: nessun P&L, nessuna fee
            state["last_rebalance"] = h["timestamp"]
            continue

        entry_price = (h.get("prices_snapshot") or {}).get(symbol)
        if entry_price is None:
            print(f"  [WARN] Prezzo di entrata mancante per {symbol} il {h['timestamp']}, salto segmento.")
            state["last_rebalance"] = h["timestamp"]
            continue

        capital = capital * (1 - FEE_PER_SIDE)  # fee di ingresso
        print(f"  Segmento {i+1}: ENTER {symbol} @ ${entry_price:,.2f} il {h['timestamp']}"
              f"  (cap dopo fee ${capital:,.2f})")

        stop_price = entry_price * (1 - STOP_LOSS_PCT)

        try:
            days_span = max(2, int((end_ts - start_ts).total_seconds() / 86400) + 3)
            candles = base.fetch_daily_candles(symbol, days_back=days_span)
        except Exception as e:
            print(f"  [WARN] Impossibile scaricare candele storiche per {symbol}: {e}")
            candles = []

        start_ms, end_ms = start_ts.timestamp() * 1000, end_ts.timestamp() * 1000
        segment_candles = [c for c in candles if start_ms <= c["ts"] <= end_ms]

        stopped = False
        for c in segment_candles:
            if c["low"] <= stop_price:
                pnl_pct = (stop_price / entry_price - 1) * LEVERAGE
                capital = capital * (1 + pnl_pct) * (1 - FEE_PER_SIDE)
                stop_date = datetime.fromtimestamp(c["ts"] / 1000, tz=timezone.utc)
                state["trade_count"] += 1
                state["stop_count"] += 1
                print(f"    >>> STOP-LOSS toccato il {stop_date.date()} "
                      f"(low ${c['low']:,.2f} <= stop ${stop_price:,.2f})  "
                      f"P&L leva: {pnl_pct*100:+.2f}%  cap=${capital:,.2f}")
                append_log(stop_date.isoformat(), "stop_loss", symbol, capital, {},
                           note=f"pnl_leva={pnl_pct*100:+.2f}% (backfill)")
                state["history"].append({
                    "timestamp": stop_date.isoformat(), "event": "stop_loss",
                    "from": symbol, "to": None, "capital_after": capital,
                })
                state["current_symbol"] = None
                state["entry_price"] = None
                state["capital_at_entry"] = capital
                stopped = True
                break

        if not stopped:
            is_last_segment = (i + 1 == len(history))
            if is_last_segment:
                # posizione ancora aperta oggi: la lascio aperta, il mark-to-market
                # e l'eventuale check dello stop per "oggi" li fa run_once() con i
                # prezzi live, non serve approssimarli qui.
                state["current_symbol"] = symbol
                state["entry_price"] = entry_price
                state["entry_time"] = h["timestamp"]
                state["capital_at_entry"] = capital
                print(f"    Posizione ancora aperta ({symbol}), verra' valutata con i prezzi live.")
            else:
                # prezzo di uscita reale = prezzo registrato dal bot base al rebalance successivo
                exit_price = (history[i + 1].get("prices_snapshot") or {}).get(
                    symbol, segment_candles[-1]["close"] if segment_candles else entry_price)
                pnl_pct = (exit_price / entry_price - 1) * LEVERAGE
                capital = capital * (1 + pnl_pct) * (1 - FEE_PER_SIDE)
                state["trade_count"] += 1
                if pnl_pct > 0:
                    state["win_count"] += 1
                print(f"    EXIT (rebalance normale) @ ~${exit_price:,.2f}  "
                      f"P&L leva: {pnl_pct*100:+.2f}%  cap=${capital:,.2f}")
                append_log(end_ts.isoformat(), "exit", symbol, capital, {},
                           note=f"pnl_leva={pnl_pct*100:+.2f}% (backfill)")
                state["current_symbol"] = None
                state["entry_price"] = None
                state["capital_at_entry"] = capital

        state["last_rebalance"] = h["timestamp"]

    save_state(state)
    print(f"[BACKFILL] Completato. Capitale ricostruito: ${capital:,.2f}\n")
    return state


# ======================================================================
# RUN: stop-loss check + rebalance settimanale, con prezzi live
# ======================================================================
def run_once():
    now = datetime.now(timezone.utc)
    state = load_state()
    if state is None:
        state = backfill_from_baseline()
        if state is None:
            state = init_state(base.DEFAULT_CAPITAL)
            save_state(state)
            print("[INIT] Nessuno storico del bot base trovato: parto da zero.")

    print(f"\n=== Paper Trading A LEVA ({LEVERAGE}x, stop {STOP_LOSS_PCT*100:.0f}%) ===")
    print(f"Timestamp UTC: {now.isoformat(timespec='seconds')}")

    price_history = base.fetch_all_prices()
    missing = [s for s, v in price_history.items() if v is None]
    if missing:
        print(f"[ERROR] Dati mancanti per: {missing}. Mi fermo.")
        sys.exit(1)
    current_prices = {sym: candles[-1]["close"] for sym, candles in price_history.items()}

    # --- 1. Check stop-loss su posizione aperta (prioritario, a prescindere dal timer) ---
    if state["current_symbol"]:
        cur = current_prices[state["current_symbol"]]
        entry = state["entry_price"]
        price_pnl = (cur / entry - 1)
        if price_pnl <= -STOP_LOSS_PCT:
            exit_price = cur * (1 - SLIPPAGE)
            realized_price_pct = (exit_price / entry - 1)
            leveraged_pnl = realized_price_pct * LEVERAGE
            new_cap = state["capital_at_entry"] * (1 + leveraged_pnl) * (1 - FEE_PER_SIDE)
            print(f"  >>> STOP-LOSS TOCCATO su {state['current_symbol']}: "
                  f"prezzo {price_pnl*100:+.2f}% (leva: {leveraged_pnl*100:+.2f}%)  cap=${new_cap:,.2f}")
            state["trade_count"] += 1
            state["stop_count"] = state.get("stop_count", 0) + 1
            append_log(now.isoformat(), "stop_loss", state["current_symbol"], new_cap, current_prices,
                       note=f"pnl_leva={leveraged_pnl*100:+.2f}%")
            state["history"].append({
                "timestamp": now.isoformat(), "event": "stop_loss",
                "from": state["current_symbol"], "to": None, "capital_after": new_cap,
                "prices_snapshot": current_prices,
            })
            base.send_telegram(
                f"🛑 <b>STOP-LOSS (leva {LEVERAGE:.0f}x)</b>\n"
                f"Uscita da {state['current_symbol']} @ ${exit_price:,.2f}\n"
                f"P&L: {leveraged_pnl*100:+.2f}%  |  Capitale: ${new_cap:,.2f}"
            )
            state["current_symbol"] = None
            state["entry_price"] = None
            state["entry_time"] = None
            state["capital_at_entry"] = new_cap
            save_state(state)

    # --- 2. Mark-to-market + segnale (informativo) ---
    mtm = state["capital_at_entry"]
    if state["current_symbol"]:
        cur = current_prices[state["current_symbol"]]
        mtm = state["capital_at_entry"] * (1 + (cur / state["entry_price"] - 1) * LEVERAGE)
    total_return = (mtm / state["initial_capital"] - 1) * 100
    target, momentums = base.compute_signal(price_history)

    print(f"  Capitale (leva {LEVERAGE}x): ${mtm:,.2f}  (rendimento totale {total_return:+.2f}%)")
    print(f"  Posizione: {state['current_symbol'] or 'CASH'}  |  Segnale target: {target or 'CASH'}")

    # --- 3. Rebalance settimanale (stesso timer/segnale del bot base) ---
    last_rebal_iso = state.get("last_rebalance")
    if last_rebal_iso:
        last_dt = parse_iso(last_rebal_iso)
        days_since = (now - last_dt).total_seconds() / 86400
        needs_rebal = days_since >= REBALANCE_DAYS
    else:
        needs_rebal = True
        days_since = None

    if not needs_rebal:
        next_rebal_days = REBALANCE_DAYS - days_since
        print(f"  Non e' ancora tempo di rebalance (prossimo tra {next_rebal_days:.2f} giorni)")
        append_log(now.isoformat(), "monitor", state["current_symbol"], mtm, current_prices,
                   note=f"target={target}, days_to_rebal={next_rebal_days:.2f}")
        save_state(state)
        return

    old_sym = state["current_symbol"]
    if old_sym == target:
        state["last_rebalance"] = now.isoformat()
        append_log(now.isoformat(), "rebalance_hold", target, mtm, current_prices, note="no change")
        save_state(state)
        print(f"  Rebalance: nessun cambio, resto su {target or 'CASH'}")
        return

    cap = state["capital_at_entry"]
    notify_lines = []
    if old_sym:
        exit_price = current_prices[old_sym] * (1 - SLIPPAGE)
        pnl_pct = (exit_price / state["entry_price"] - 1) * LEVERAGE
        cap = cap * (1 + pnl_pct) * (1 - FEE_PER_SIDE)
        state["trade_count"] += 1
        if pnl_pct > 0:
            state["win_count"] += 1
        print(f"  EXIT  {old_sym} @ ${exit_price:,.2f}  P&L leva: {pnl_pct*100:+.2f}%  cap=${cap:,.2f}")
        append_log(now.isoformat(), "exit", old_sym, cap, current_prices, note=f"pnl_leva={pnl_pct*100:+.2f}%")
        notify_lines.append(f"EXIT {old_sym} (P&L leva {pnl_pct*100:+.2f}%)")

    if target:
        entry_price = current_prices[target] * (1 + SLIPPAGE)
        cap = cap * (1 - FEE_PER_SIDE)
        state["current_symbol"] = target
        state["entry_price"] = entry_price
        state["entry_time"] = now.isoformat()
        state["capital_at_entry"] = cap
        print(f"  ENTER {target} @ ${entry_price:,.2f}  cap=${cap:,.2f}")
        append_log(now.isoformat(), "enter", target, cap, current_prices, note=f"entry_price={entry_price:.2f}")
        notify_lines.append(f"ENTER {target} @ ${entry_price:,.2f}")
    else:
        state["current_symbol"] = None
        state["entry_price"] = None
        state["entry_time"] = None
        state["capital_at_entry"] = cap
        print(f"  ALLOCATE TO CASH  cap=${cap:,.2f}")
        append_log(now.isoformat(), "to_cash", None, cap, current_prices, note="no eligible asset")
        notify_lines.append("ALLOCATE TO CASH")

    base.send_telegram(
        f"⚡ <b>Rebalance (leva {LEVERAGE:.0f}x)</b>\n" + "\n".join(notify_lines) + f"\nCapitale: ${cap:,.2f}"
    )

    state["history"].append({
        "timestamp": now.isoformat(), "event": "rebalance",
        "from": old_sym, "to": target, "capital_after": cap,
        "prices_snapshot": current_prices,
    })
    state["last_rebalance"] = now.isoformat()
    save_state(state)

    print(f"\n=== Summary (leva {LEVERAGE}x) ===")
    print(f"Capitale: ${state['capital_at_entry']:,.2f}")
    print(f"Posizione: {state['current_symbol'] or 'CASH'}")
    print(f"Trade totali: {state['trade_count']}  (di cui stop-loss: {state.get('stop_count', 0)})")


if __name__ == "__main__":
    run_once()
