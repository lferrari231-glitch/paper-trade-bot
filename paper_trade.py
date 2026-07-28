"""
======================================================================
 Paper Trading - Naive Doppio Momentum su Hyperliquid
======================================================================
Simula la strategia Naive (lookback 14d/30d, rebalance settimanale)
con dati reali da Hyperliquid in tempo reale, ma SENZA SOLDI VERI.

USO:
  python3 paper_trade.py                       # esecuzione normale
  python3 paper_trade.py --reset               # reset stato
  python3 paper_trade.py --reset --capital 5000  # reset con capitale custom
  python3 paper_trade.py --status              # solo monitora, no decisioni
  python3 paper_trade.py --history             # mostra storico operazioni

COSA FA OGNI VOLTA CHE LO LANCI:
  1. Scarica prezzi correnti + ultimi 60 giorni di candele daily
     di BTC, ETH, SOL, BNB direttamente dall'API Hyperliquid
  2. Aggiorna mark-to-market del portafoglio
  3. Se sono passati >=7 giorni dall'ultimo rebalance:
       - Calcola momentum 14d e 30d per ogni asset
       - Sceglie l'asset con miglior momentum 30d tra quelli con
         entrambi i momentum > 0 (Naive Doppio Momentum)
       - "Compra" o "vende" virtualmente, applicando fee 0.035% taker
  4. Logga tutto in paper_state.json e paper_log.csv

FREQUENZA SUGGERITA:
  Lancialo una volta al giorno (esempio: ogni sera). Solo una volta a
  settimana effettivamente ribilancia; gli altri giorni e' solo monitoring.
"""

import os
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone

# ======================================================================
# CONFIG
# ======================================================================
SYMBOLS         = ["BTC", "ETH", "SOL", "BNB"]
LOOKBACK_SHORT  = 14
LOOKBACK_LONG   = 30
REBALANCE_DAYS  = 7
FEE_PER_SIDE    = 0.00035       # Hyperliquid taker
SLIPPAGE        = 0.0001
DEFAULT_CAPITAL = 5000          # capitale virtuale iniziale
API_URL         = "https://api.hyperliquid.xyz/info"

STATE_FILE = "paper_state.json"
LOG_FILE   = "paper_log.csv"

LEVERAGE_STATE_FILE = "paper_state_leverage.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")


# ======================================================================
# 0. NOTIFICHE TELEGRAM (opzionali: attive solo se le env var sono settate)
# ======================================================================
def leverage_summary_line():
    """Riga di riepilogo del bot a leva (se il file esiste), da aggiungere in
    coda ai messaggi Telegram del bot base per avere il confronto in un unico posto."""
    if not os.path.exists(LEVERAGE_STATE_FILE):
        return "⚡ Leva 2x: non ancora avviato"
    try:
        with open(LEVERAGE_STATE_FILE) as f:
            lev = json.load(f)
        cap = lev.get("capital_at_entry")
        initial = lev.get("initial_capital")
        pos = lev.get("current_symbol") or "CASH"
        stop_count = lev.get("stop_count", 0)
        ret = (cap / initial - 1) * 100 if cap and initial else 0
        extra = f", {stop_count} stop-loss finora" if stop_count else ""
        return f"⚡ Leva 2x: {pos} | ${cap:,.2f} ({ret:+.2f}%{extra})"
    except Exception as e:
        return f"⚡ Leva 2x: dati non leggibili ({e})"


SEND_PHOTO_FLAG_FILE = ".send_photo_flag"


def send_telegram(text):
    """Invia un messaggio Telegram se TELEGRAM_BOT_TOKEN/CHAT_ID sono configurati.
    Non fa mai fallire lo script: eventuali errori vengono solo stampati."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    # Segna che e' stato mandato un messaggio "vero" (non il monitoring silenzioso
    # del bot a leva): il workflow legge questo flag per decidere se allegare anche
    # il grafico aggiornato in uno step successivo (dopo il commit/push).
    try:
        with open(SEND_PHOTO_FLAG_FILE, "w") as f:
            f.write("1")
    except Exception:
        pass
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"  [WARN] Notifica Telegram non inviata: {e}")


def send_telegram_photo(caption, filename="comparison_chart.png"):
    """Manda il grafico come foto Telegram, leggendolo dal repo GitHub pubblico
    (raw.githubusercontent.com), cosi' non serve caricare/allegare il file a mano.
    Funziona solo quando lo script gira dentro una GitHub Action (dove GITHUB_REPOSITORY
    e' impostata automaticamente); in locale viene saltato senza errori."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return
    photo_url = f"https://raw.githubusercontent.com/{repo}/main/{filename}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"  [WARN] Invio grafico Telegram non riuscito: {e}")


def compute_sma(candles, n):
    """Media mobile semplice sugli ultimi n giorni di chiusura."""
    if candles is None or len(candles) < n:
        return None
    closes = [c["close"] for c in candles[-n:]]
    return sum(closes) / n


def build_market_table(price_history, current_prices):
    """Tabellina monospace (prezzo, SMA14, SMA30 per asset) per il messaggio Telegram,
    per avere un colpo d'occhio sulla situazione di mercato al posto delle sole
    spunte di eligibilita'."""
    def fmt(v):
        return f"{v:,.0f}" if v is not None else "n/d"

    lines = [f"{'':4s}{'Prezzo':>9s}{'SMA14':>9s}{'SMA30':>9s}"]
    for sym in SYMBOLS:
        candles = price_history.get(sym)
        sma14 = compute_sma(candles, LOOKBACK_SHORT)
        sma30 = compute_sma(candles, LOOKBACK_LONG)
        price = current_prices.get(sym)
        lines.append(f"{sym:4s}{fmt(price):>9s}{fmt(sma14):>9s}{fmt(sma30):>9s}")
    return "<pre>" + "\n".join(lines) + "</pre>"


# ======================================================================
# 1. HYPERLIQUID API
# ======================================================================
def hl_request(body):
    """POST request al /info endpoint di Hyperliquid."""
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def fetch_daily_candles(coin, days_back=60):
    """Scarica le ultime N candele daily da Hyperliquid."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days_back * 24 * 60 * 60 * 1000
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    candles = hl_request(body)
    # ogni candela: {"t": open_ts, "T": close_ts, "s": symbol, "o", "c", "h", "l", "v", "n"}
    return sorted(
        [{"ts": int(c["t"]), "open": float(c["o"]), "close": float(c["c"]),
          "high": float(c.get("h", c["c"])), "low": float(c.get("l", c["c"]))}
         for c in candles],
        key=lambda x: x["ts"],
    )


def fetch_all_prices():
    """Per ogni asset, scarica storico daily + prezzo corrente."""
    result = {}
    for sym in SYMBOLS:
        try:
            candles = fetch_daily_candles(sym, days_back=LOOKBACK_LONG + 10)
            result[sym] = candles
        except Exception as e:
            print(f"  [WARN] Errore scarico {sym}: {e}")
            result[sym] = None
    return result


# ======================================================================
# 2. STRATEGY LOGIC: NAIVE DOPPIO MOMENTUM
# ======================================================================
def compute_signal(price_history):
    """
    Applica Naive Doppio Momentum:
      - Per ogni asset calcola momentum 14d e 30d
      - Asset eligibile solo se ENTRAMBI > 0
      - Tra gli eligibili, sceglie quello con momentum 30d maggiore
      - Se nessuno eligibile, ritorna None (cash)
    Ritorna: (target_symbol or None, dict di momenti per ogni asset)
    """
    momentums = {}
    eligible = []
    for sym, candles in price_history.items():
        if candles is None or len(candles) < LOOKBACK_LONG + 1:
            momentums[sym] = {"mom_short": None, "mom_long": None, "eligible": False}
            continue
        prices = [c["close"] for c in candles]
        cur = prices[-1]
        p_short = prices[-1 - LOOKBACK_SHORT]
        p_long  = prices[-1 - LOOKBACK_LONG]
        m_short = (cur - p_short) / p_short
        m_long  = (cur - p_long) / p_long
        elig = (m_short > 0) and (m_long > 0)
        momentums[sym] = {"mom_short": m_short, "mom_long": m_long, "eligible": elig}
        if elig:
            eligible.append((sym, m_long))

    if not eligible:
        return None, momentums
    eligible.sort(key=lambda x: -x[1])
    return eligible[0][0], momentums


# ======================================================================
# 3. STATE MANAGEMENT
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
        "initial_capital":   capital,
        "capital_at_entry":  capital,    # capitale al momento dell'ultima entrata/uscita
        "current_symbol":    None,
        "entry_price":       None,
        "entry_time":        None,
        "last_rebalance":    None,
        "trade_count":       0,
        "win_count":         0,
        "history":           [],         # lista di rebalance events
    }


def compute_mtm_capital(state, current_prices):
    """Capitale 'mark-to-market': se in posizione, applica la variazione di prezzo."""
    if state["current_symbol"] is None:
        return state["capital_at_entry"]
    cur = current_prices.get(state["current_symbol"])
    if cur is None or state["entry_price"] is None:
        return state["capital_at_entry"]
    return state["capital_at_entry"] * (cur / state["entry_price"])


# ======================================================================
# 4. LOG
# ======================================================================
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


# ======================================================================
# 5. MAIN
# ======================================================================
def main():
    # Parse args
    args = sys.argv[1:]
    reset = "--reset" in args
    status_only = "--status" in args
    show_history = "--history" in args
    capital_arg = None
    if "--capital" in args:
        i = args.index("--capital")
        if i + 1 < len(args):
            capital_arg = float(args[i + 1])

    state = load_state()
    if state is None or reset:
        cap = capital_arg if capital_arg else DEFAULT_CAPITAL
        state = init_state(cap)
        print(f"[INIT] Nuovo stato con capitale virtuale ${cap:,.2f}")
        save_state(state)

    if show_history:
        print(f"\n=== Storico operazioni ({len(state['history'])} eventi) ===")
        for h in state["history"]:
            print(f"  {h['timestamp']} | {h['event']:12s} | "
                  f"{h.get('symbol', '-'):4s} | "
                  f"cap ${h.get('capital_after', 0):,.2f}")
        return

    now = datetime.now(timezone.utc)
    print(f"\n=== Paper Trading Naive Doppio Momentum ===")
    print(f"Timestamp UTC: {now.isoformat(timespec='seconds')}")
    print(f"File stato:    {STATE_FILE}")
    print(f"File log:      {LOG_FILE}")

    # Fetch dati
    print(f"\n[1/4] Scarico storico daily da Hyperliquid (60 giorni)...")
    price_history = fetch_all_prices()
    missing = [s for s, v in price_history.items() if v is None]
    if missing:
        print(f"[ERROR] Dati mancanti per: {missing}. Mi fermo.")
        sys.exit(1)
    current_prices = {sym: candles[-1]["close"] for sym, candles in price_history.items()}
    print("Prezzi correnti:")
    for sym, p in current_prices.items():
        print(f"  {sym:4s}: ${p:>12,.2f}")

    # Mark-to-market
    mtm = compute_mtm_capital(state, current_prices)
    total_return = (mtm / state["initial_capital"] - 1) * 100
    print(f"\n[2/4] Mark-to-market")
    print(f"  Capitale virtuale corrente: ${mtm:,.2f}")
    print(f"  Capitale iniziale:          ${state['initial_capital']:,.2f}")
    print(f"  Rendimento totale:          {total_return:+.2f}%")
    if state["current_symbol"]:
        cur = current_prices[state["current_symbol"]]
        ent = state["entry_price"]
        unrealized = (cur / ent - 1) * 100
        print(f"  Posizione aperta:           {state['current_symbol']} @ ${ent:,.2f} "
              f"(P&L non realizzato: {unrealized:+.2f}%)")
    else:
        print(f"  Posizione aperta:           CASH")

    # Compute signal (sempre, anche se non rebalance, per informazione)
    print(f"\n[3/4] Calcolo segnale Naive Doppio Momentum (14d + 30d)")
    target, momentums = compute_signal(price_history)
    for sym in SYMBOLS:
        m = momentums[sym]
        if m["mom_short"] is None:
            print(f"  {sym:4s}: dati insufficienti")
            continue
        flag = "OK" if m["eligible"] else "--"
        print(f"  {sym:4s}: mom14d {m['mom_short']*100:+6.2f}%  "
              f"mom30d {m['mom_long']*100:+6.2f}%  [{flag}]")
    print(f"  --> Segnale target: {target or 'CASH (nessun asset eligibile)'}")

    # Decide rebalance
    print(f"\n[4/4] Decisione di rebalance")
    last_rebal_iso = state.get("last_rebalance")
    if last_rebal_iso:
        last_dt = datetime.fromisoformat(last_rebal_iso.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        days_since = (now - last_dt).total_seconds() / 86400
        needs_rebal = days_since >= REBALANCE_DAYS
        print(f"  Ultimo rebalance:  {last_rebal_iso} ({days_since:.2f} giorni fa)")
    else:
        needs_rebal = True
        days_since = None
        print(f"  Primo rebalance (mai eseguito prima)")

    if status_only:
        print("  [STATUS-ONLY] Skip decisione, solo monitoring.")
        append_log(now.isoformat(), "monitor",
                   state["current_symbol"], mtm, current_prices,
                   note=f"target={target}")
        save_state(state)
        return

    if not needs_rebal:
        next_rebal_days = REBALANCE_DAYS - days_since
        print(f"  Non e' ancora tempo di rebalance (prossimo tra {next_rebal_days:.2f} giorni)")
        append_log(now.isoformat(), "monitor",
                   state["current_symbol"], mtm, current_prices,
                   note=f"target={target}, days_to_rebal={next_rebal_days:.2f}")
        save_state(state)
        market_table = build_market_table(price_history, current_prices)
        send_telegram(
            f"📈 <b>Monitor giornaliero</b>\n\n"
            f"<b>Base</b>: {state['current_symbol'] or 'CASH'} | ${mtm:,.2f} ({total_return:+.2f}%)\n"
            f"{leverage_summary_line()}\n\n"
            f"<b>Mercato</b>\n{market_table}\n"
            f"Prossimo rebalance tra {next_rebal_days:.1f}g (target: {target or 'CASH'})"
        )
        # NB: il grafico viene inviato da uno step separato del workflow DOPO il
        # commit/push, cosi' raw.githubusercontent.com serve gia' la versione fresca
        # (vedi paper_trade.yml / paper_trade_leverage.yml).
        return

    # =========== ESEGUI REBALANCE VIRTUALE ===========
    print(f"  >>> REBALANCE TIME <<<")
    old_sym = state["current_symbol"]

    if old_sym == target:
        print(f"  Posizione target == attuale ({target or 'CASH'}): nessun trade")
        state["last_rebalance"] = now.isoformat()
        append_log(now.isoformat(), "rebalance_hold",
                   target, mtm, current_prices,
                   note="no change")
        market_table = build_market_table(price_history, current_prices)
        send_telegram(
            f"📊 <b>Rebalance</b>: nessun cambio, resto su <b>{target or 'CASH'}</b>\n\n"
            f"<b>Base</b>: ${mtm:,.2f} ({total_return:+.2f}%)\n"
            f"{leverage_summary_line()}\n\n"
            f"<b>Mercato</b>\n{market_table}"
        )
    else:
        # Chiudi posizione corrente (se c'e')
        cap = state["capital_at_entry"]
        notify_lines = []
        if old_sym:
            exit_price = current_prices[old_sym] * (1 - SLIPPAGE)  # slippage in uscita
            pnl_pct = (exit_price / state["entry_price"] - 1)
            cap = cap * (1 + pnl_pct) * (1 - FEE_PER_SIDE)
            won = pnl_pct > 0
            state["trade_count"] += 1
            if won:
                state["win_count"] += 1
            print(f"  EXIT  {old_sym:4s} @ ${exit_price:,.2f}  P&L: {pnl_pct*100:+.2f}%  cap=${cap:,.2f}")
            append_log(now.isoformat(), "exit",
                       old_sym, cap, current_prices,
                       note=f"pnl={pnl_pct*100:+.2f}%")
            notify_lines.append(f"EXIT {old_sym} @ ${exit_price:,.2f} (P&L {pnl_pct*100:+.2f}%)")

        # Apri nuova posizione (se target != CASH)
        if target:
            entry_price = current_prices[target] * (1 + SLIPPAGE)
            cap = cap * (1 - FEE_PER_SIDE)
            state["current_symbol"]   = target
            state["entry_price"]      = entry_price
            state["entry_time"]       = now.isoformat()
            state["capital_at_entry"] = cap
            print(f"  ENTER {target:4s} @ ${entry_price:,.2f}  cap=${cap:,.2f}")
            append_log(now.isoformat(), "enter",
                       target, cap, current_prices,
                       note=f"entry_price={entry_price:.2f}")
            notify_lines.append(f"ENTER {target} @ ${entry_price:,.2f}")
        else:
            state["current_symbol"]   = None
            state["entry_price"]      = None
            state["entry_time"]       = None
            state["capital_at_entry"] = cap
            print(f"  ALLOCATE TO CASH  cap=${cap:,.2f}")
            append_log(now.isoformat(), "to_cash",
                       None, cap, current_prices,
                       note="no eligible asset")
            notify_lines.append("ALLOCATE TO CASH (nessun asset eligibile)")

        market_table = build_market_table(price_history, current_prices)
        send_telegram(
            "🔁 <b>Rebalance eseguito (base)</b>\n\n" + "\n".join(notify_lines) +
            f"\nCapitale: ${cap:,.2f}\n{leverage_summary_line()}\n\n"
            f"<b>Mercato</b>\n{market_table}"
        )

        state["history"].append({
            "timestamp":        now.isoformat(),
            "event":            "rebalance",
            "from":             old_sym,
            "to":               target,
            "capital_after":    cap,
            "prices_snapshot":  current_prices,
        })
        state["last_rebalance"] = now.isoformat()

    save_state(state)

    print(f"\n=== Summary ===")
    print(f"Capitale: ${state['capital_at_entry']:,.2f}")
    print(f"Posizione: {state['current_symbol'] or 'CASH'}")
    print(f"Operazioni totali: {state['trade_count']} (win: {state['win_count']}, "
          f"win rate: {(state['win_count']/state['trade_count']*100 if state['trade_count']>0 else 0):.1f}%)")


if __name__ == "__main__":
    main()

