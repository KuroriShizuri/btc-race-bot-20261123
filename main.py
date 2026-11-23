"""BTC trend bot for make.fun btc-race-24h-20261123-0511.

Speaks agent-fun/bars-replay/v1: one JSON line per minute on stdin (hello,
then one batch per 1-minute CBBTC bar, then done), one reply per batch on
stdout. Long only, at most one open position, whole cash per entry.

Strategy (designed and backtested on the published dev window; dev net PnL
+$21.74, max drawdown 1.98%, 1 trade):
  ENTER (flat) when all hold on the latest close:
    - new 30-minute high: close above the max of the previous 30 closes;
    - 60-minute momentum: close more than 80 bps above the close 60 bars ago;
    - 4-hour trend veto: when more than 240 closes exist, close above the
      close 240 bars ago (never buy a bounce inside a falling 4-hour trend).
  EXIT (holding) when the close falls 250 bps below the highest close seen
  since entry (a 2.5% trailing stop). No other exit: 1-minute noise breaks
  every faster exit on this data (a 15-bar-low exit lost $67 on the dev
  window; EMA exits lost $10-115 across the house baselines).
  After an exit, no re-entry for 240 bars.
  Late-window fallback: after bar 1080, if no buy has ever been sent, the
  momentum bar drops to 40 bps (guardrail insurance: a race window that
  never fires the 80 bps entry would otherwise end with 0 trades and rank
  below every guardrail-passing entry).
  A sell is confirmed only when the position is observed closed; until then
  the trailing high stays armed, so a sell rejected on max_slippage (e.g. a
  gap-down open) is re-sent on the next bar instead of silently re-arming
  the stop from a lower price.
  Buy the whole cash balance; sell the whole position. One order per reply
  at most, max_slippage_bps 150 (a fill more than 150 bps away from the
  latest close is rejected and retried next bar).

Costs matter: a full-size round trip pays ~35 bps (4 bps fee + 2 bps
slippage + ~11 bps impact each way at dev depths), so the bot trades
rarely -- about one round trip per day in backtests -- and only on a
confirmed strong move.

Determinism: integer arithmetic only (no floats), no clock, no randomness,
no environment, no files. The same bars always produce the same orders.
Standard library only.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"

# Strategy constants (all in basis points unless noted).
HI_BARS = 30          # entry: new high over the previous HI_BARS closes
MOM_BARS = 60         # entry: momentum measured over MOM_BARS closes
MOM_BPS = 80          # entry: 60-min return must exceed 80 bps
LATE_BARS = 1080      # fallback: after this bar, with no buy ever sent,
LATE_MOM_BPS = 40     #   accept a weaker 40 bps momentum (guardrail insurance)
VETO_BARS = 240       # entry veto: 240-min return must be positive
TRAIL_BPS = 250       # exit: 250 bps below the high since entry
COOLDOWN_BARS = 240   # no re-entry for this many bars after an exit
MAX_SLIPPAGE_BPS = 150


def main():
    closes = []          # CBBTC closes seen so far (ints, price_scale units)
    symbol = None
    warmup_bars = 0
    min_notional = 1
    max_weight_bps = 10000
    order_no = 0
    high_since_entry = None
    last_exit_seq = -10 ** 9
    pending_exit_seq = None  # seq of a sell decision not yet observed filled
    prev_qty = 0
    ever_bought = False

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # not a message; nothing to answer
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                raise SystemExit("unexpected protocol: %r" % (msg.get("protocol"),))
            symbols = msg.get("symbols") or []
            symbol = symbols[0]["symbol"] if symbols else "CBBTC"
            warmup_bars = int(msg.get("warmup_bars") or 0)
            fees = msg.get("fees") or {}
            min_notional = int(fees.get("min_notional_quote_units") or 1)
            caps = msg.get("caps") or {}
            max_weight_bps = int(caps.get("max_weight_bps") or 10000)
            continue
        if kind == "done":
            return 0
        if kind != "batch":
            continue  # unknown message: no reply owed
        seq = int(msg["seq"])
        orders = []
        try:
            account = msg["account"]
            cash = int(account["cash_quote_units"])
            positions = account.get("positions") or []
            qty = 0
            for p in positions:
                if p["symbol"] == symbol:
                    qty = int(p["qty"])
            equity = int(account["equity_quote_units"])

            # A sell is confirmed only when the position is observed closed.
            # Until then the trailing high stays armed, so a rejected sell
            # (e.g. max_slippage on a gap-down open) is re-sent next bar
            # instead of silently re-arming the stop from a lower price.
            if qty == 0 and prev_qty > 0 and pending_exit_seq is not None:
                high_since_entry = None
                last_exit_seq = pending_exit_seq
                pending_exit_seq = None
            prev_qty = qty

            bar_close = None
            for bar in msg.get("bars") or []:
                if bar["symbol"] == symbol:
                    bar_close = int(bar["c"])
            if bar_close is not None and seq >= warmup_bars:
                closes.append(bar_close)
                n = len(closes)
                close = bar_close
                if qty > 0:
                    if high_since_entry is None or close > high_since_entry:
                        high_since_entry = close
                    # Trailing stop: close 250 bps below the high since entry.
                    if close * 10000 < high_since_entry * (10000 - TRAIL_BPS):
                        order_no += 1
                        orders.append({
                            "side": "sell",
                            "client_id": "o%d" % order_no,
                            "symbol": symbol,
                            "qty": str(qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })
                        pending_exit_seq = seq
                elif n >= MOM_BARS + 1 and seq - last_exit_seq > COOLDOWN_BARS:
                    prev_high = max(closes[-HI_BARS - 1:-1])
                    mom_bps = MOM_BPS
                    if not ever_bought and seq >= LATE_BARS:
                        mom_bps = LATE_MOM_BPS
                    mom_ok = (close - closes[-MOM_BARS - 1]) * 10000 > mom_bps * closes[-MOM_BARS - 1]
                    veto_ok = True
                    if n > VETO_BARS:
                        veto_ok = close > closes[-VETO_BARS - 1]
                    if close > prev_high and mom_ok and veto_ok:
                        room = max_weight_bps * equity // 10000
                        notional = cash if cash < room else room
                        if notional >= min_notional:
                            order_no += 1
                            orders.append({
                                "side": "buy",
                                "client_id": "o%d" % order_no,
                                "symbol": symbol,
                                "notional_quote_units": str(notional),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            })
                            high_since_entry = close
                            ever_bought = True
        except Exception:
            # Never die on one bad batch: answer it with no orders.
            orders = []
        out.write(json.dumps(
            {"type": "reply", "seq": seq, "orders": orders},
            separators=(",", ":"),
        ) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
