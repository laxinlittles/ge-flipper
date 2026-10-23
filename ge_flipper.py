#!/usr/bin/env python3
"""
OSRS Grand Exchange Flip Finder
================================
Scans live GE prices from the OSRS Wiki real-time prices API and alerts you
to items with a strong flip margin after GE tax.

For each opportunity it reports:
  - Buy price (place your buy offer here)
  - Sell price (place your sell offer here)
  - Quantity to buy (respects buy limit, your budget, and real trade volume)
  - GE tax on the sale
  - Profit per item and total profit for the flip, AFTER tax

Alerts go to the console, and optionally to your phone (ntfy.sh) and/or Discord.

Requirements:  pip install requests
Run:           python ge_flipper.py --budget 10000000
Help:          python ge_flipper.py --help
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

API = "https://prices.runescape.wiki/api/v1/osrs"
# The Wiki asks every tool to send a descriptive User-Agent. Put your Discord/email here.
USER_AGENT = "ge_flipper personal merching tool - contact: your_discord_or_email"

TAX_CAP = 5_000_000          # GE tax is capped at 5M coins per item
TAX_EXEMPT_IDS = {13190}     # Old school bond (tax-free). Add others if needed.
ALERT_LOG = "ge_alert_history.json"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def fetch(endpoint):
    r = requests.get(f"{API}/{endpoint}", headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def load_mapping():
    """Item id -> {name, limit, members}. Cached for the session."""
    return {item["id"]: item for item in fetch("mapping")}


# --------------------------------------------------------------------------- #
# Math
# --------------------------------------------------------------------------- #
def ge_tax(sell_price, item_id, rate):
    """Per-item GE tax: floor(price * rate), capped, with exemptions."""
    if item_id in TAX_EXEMPT_IDS:
        return 0
    return min(math.floor(sell_price * rate), TAX_CAP)


def analyze(mapping, latest, hourly, cfg):
    now = time.time()
    results = []

    for sid, p in latest.items():
        item_id = int(sid)
        info = mapping.get(item_id)
        if not info:
            continue
        if cfg.f2p and info.get("members"):
            continue

        insta_buy = p.get("high")     # price people are instantly buying at
        insta_sell = p.get("low")     # price people are instantly selling at
        if not insta_buy or not insta_sell:
            continue

        # Skip stale prices - a margin from 3 hours ago isn't real.
        max_age = cfg.max_age * 60
        if now - (p.get("highTime") or 0) > max_age or now - (p.get("lowTime") or 0) > max_age:
            continue

        # Your offers sit just inside the spread so they fill ahead of others.
        buy_price = insta_sell + 1
        sell_price = insta_buy - 1
        if sell_price <= buy_price:
            continue

        if buy_price < cfg.min_price or buy_price > cfg.budget:
            continue

        tax = ge_tax(sell_price, item_id, cfg.tax)
        profit_each = sell_price - tax - buy_price
        if profit_each <= 0:
            continue

        roi = profit_each / buy_price * 100
        if roi < cfg.min_roi:
            continue
        # Absurd ROI on a liquid item usually means a manipulated or stale price.
        if roi > cfg.max_roi:
            continue

        # Volume check: both sides must actually be trading.
        h = hourly.get(sid, {})
        buy_vol = h.get("highPriceVolume") or 0
        sell_vol = h.get("lowPriceVolume") or 0
        hourly_vol = min(buy_vol, sell_vol)
        if hourly_vol < cfg.min_volume:
            continue

        # Quantity: limited by buy limit (per 4h), budget, and a safe share of volume.
        limit = info.get("limit") or 0
        by_budget = cfg.budget // buy_price
        by_volume = int(hourly_vol * 4 * cfg.volume_share)   # your share of 4h of trades
        caps = [by_budget, by_volume] + ([limit] if limit else [])
        qty = max(0, min(caps))
        if qty <= 0:
            continue

        total_profit = profit_each * qty
        if total_profit < cfg.min_profit:
            continue

        results.append({
            "id": item_id,
            "name": info["name"],
            "buy": buy_price,
            "sell": sell_price,
            "qty": qty,
            "limit": limit,
            "tax": tax,
            "profit_each": profit_each,
            "total_profit": total_profit,
            "cost": buy_price * qty,
            "roi": roi,
            "hourly_vol": hourly_vol,
        })

    results.sort(key=lambda r: r["total_profit"], reverse=True)
    return results[: cfg.top]


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def gp(n):
    n = int(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}m"
    if abs(n) >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def print_table(rows, cfg):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n=== {stamp} | Top flips | budget {gp(cfg.budget)} | tax {cfg.tax * 100:g}% ===")
    if not rows:
        print("No flips meet your filters right now.")
        return
    hdr = f"{'Item':<28}{'Buy':>9}{'Sell':>9}{'Qty':>8}{'Tax/ea':>8}{'Profit/ea':>10}{'Total':>10}{'ROI':>7}{'Vol/h':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['name'][:27]:<28}{gp(r['buy']):>9}{gp(r['sell']):>9}{r['qty']:>8,}"
              f"{gp(r['tax']):>8}{gp(r['profit_each']):>10}{gp(r['total_profit']):>10}"
              f"{r['roi']:>6.1f}%{gp(r['hourly_vol']):>8}")


# --------------------------------------------------------------------------- #
# Price-history risk check (runs only on alert candidates)
# --------------------------------------------------------------------------- #
_risk_cache = {}   # item id -> (checked_at, result); avoids re-fetching every scan


def fetch_series(item_id, step):
    r = requests.get(f"{API}/timeseries", params={"id": item_id, "timestep": step},
                     headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _slope_pct(values):
    """Least-squares trend across the series, as % change from start to end."""
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return (num / den) * (n - 1) / my * 100 if my else 0.0


def assess_risk(r, cfg, hourly=None, six_hourly=None):
    """
    Checks whether price history supports this flip. Returns
    {"ok": bool, "reasons": [failed checks], "summary": str}.

    1. Below-buy risk: share of the last 48 hours where the average sell-side
       price (what buyers paid) was BELOW your buy price. If that happens often,
       you could get stuck holding items worth less than you paid.
    2. Trend: 24h price trend. A falling market makes a drop below buy likely.
    3. Spike: your buy price vs the 7-day median. Buying into a spike risks
       the price falling back below what you paid.
    4. Margin held: share of the last 24 hours the after-tax margin was positive.
       A margin that only just appeared often vanishes before you can sell.
    """
    item_id = r["id"]
    if hourly is None:
        hourly = fetch_series(item_id, "1h")
    if six_hourly is None:
        six_hourly = fetch_series(item_id, "6h")

    h48 = [p for p in hourly[-48:] if p.get("avgHighPrice")]
    h24 = [p for p in hourly[-24:] if p.get("avgHighPrice") and p.get("avgLowPrice")]
    lows7d = [p["avgLowPrice"] for p in six_hourly[-28:] if p.get("avgLowPrice")]

    if len(h48) < 12 or len(h24) < 8 or len(lows7d) < 8:
        return {"ok": False, "reasons": ["not enough price history"],
                "summary": "Not enough trade history to judge risk"}

    buy = r["buy"]
    below = sum(1 for p in h48 if p["avgHighPrice"] < buy) / len(h48)
    mids = [(p["avgHighPrice"] + p["avgLowPrice"]) / 2 for p in h24]
    trend = _slope_pct(mids)
    spike = (buy / _median(lows7d) - 1) * 100
    held = sum(1 for p in h24
               if p["avgHighPrice"] - ge_tax(p["avgHighPrice"], item_id, cfg.tax)
               - p["avgLowPrice"] > 0) / len(h24)

    reasons = []
    if below > cfg.max_drop_risk:
        reasons.append(f"price fell below buy in {below:.0%} of last 48h")
    if trend < -cfg.max_downtrend:
        reasons.append(f"24h downtrend {trend:+.1f}%")
    if spike > cfg.max_spike:
        reasons.append(f"buy price {spike:+.1f}% above 7-day median")
    if held < cfg.min_margin_held:
        reasons.append(f"margin only held {held:.0%} of last 24h")

    summary = (f"Below-buy risk: {below:.0%} of last 48h\n"
               f"24h trend: {trend:+.1f}% | vs 7d median: {spike:+.1f}%\n"
               f"Margin held: {held:.0%} of last 24h")
    return {"ok": not reasons, "reasons": reasons, "summary": summary}


def alert_text(r, risk=None):
    text = (f"{r['name']}\n"
            f"Buy at {r['buy']:,} | Sell at {r['sell']:,}\n"
            f"Qty: {r['qty']:,} (cost {gp(r['cost'])})\n"
            f"Tax: {r['tax']:,}/ea | Profit: {r['profit_each']:,}/ea\n"
            f"Total profit after tax: {r['total_profit']:,} gp ({r['roi']:.1f}% ROI)")
    if risk:
        text += "\n" + risk["summary"]
    return text


def send_alerts(rows, cfg, history):
    now = time.time()
    for r in rows:
        if r["total_profit"] < cfg.alert_profit:
            continue
        last = history.get(str(r["id"]), 0)
        if now - last < cfg.cooldown * 60:
            continue   # already alerted on this item recently

        risk = None
        if not cfg.no_risk_check:
            cached = _risk_cache.get(r["id"])
            if cached and now - cached[0] < 30 * 60 and not cached[1]["ok"]:
                continue   # failed recently; recheck in 30 min
            try:
                risk = assess_risk(r, cfg)
            except requests.RequestException as e:
                print(f"  (history check failed for {r['name']}: {e}; skipping alert)")
                continue
            _risk_cache[r["id"]] = (now, risk)
            if not risk["ok"]:
                print(f"  Skipped {r['name']} ({gp(r['total_profit'])}): "
                      + "; ".join(risk["reasons"]))
                continue

        msg = alert_text(r, risk)
        print(f"\n🔔 ALERT\n{msg}")

        if cfg.ntfy:
            try:
                requests.post(f"https://ntfy.sh/{cfg.ntfy}", data=msg.encode("utf-8"),
                              headers={"Title": f"GE flip: {r['name']}", "Tags": "moneybag"},
                              timeout=10)
            except requests.RequestException as e:
                print(f"  (ntfy failed: {e})")

        if cfg.discord:
            try:
                requests.post(cfg.discord, json={"content": f"**GE flip alert**\n```{msg}```"},
                              timeout=10)
            except requests.RequestException as e:
                print(f"  (Discord failed: {e})")

        history[str(r["id"])] = now
    save_history(history)


def load_history():
    try:
        with open(ALERT_LOG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_history(h):
    try:
        with open(ALERT_LOG, "w") as f:
            json.dump(h, f)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    a = argparse.ArgumentParser(description="OSRS GE flip finder with alerts")
    a.add_argument("--budget", type=int, default=10_000_000, help="Cash you can spend per flip (gp)")
    a.add_argument("--tax", type=float, default=0.02, help="GE tax rate (0.02 = 2%%, current rate)")
    a.add_argument("--min-roi", type=float, default=1.0, help="Minimum ROI %% after tax")
    a.add_argument("--max-roi", type=float, default=40.0, help="Ignore ROI above this (likely manipulation)")
    a.add_argument("--min-profit", type=int, default=50_000, help="Minimum total profit to list")
    a.add_argument("--alert-profit", type=int, default=250_000, help="Total profit needed to send an alert")
    a.add_argument("--min-volume", type=int, default=20, help="Min trades/hour on BOTH buy and sell side")
    a.add_argument("--volume-share", type=float, default=0.10, help="Max share of 4h volume you try to buy")
    a.add_argument("--min-price", type=int, default=10, help="Skip items cheaper than this")
    a.add_argument("--max-age", type=int, default=30, help="Ignore prices older than this (minutes)")
    a.add_argument("--top", type=int, default=15, help="How many flips to show")
    a.add_argument("--interval", type=int, default=120, help="Seconds between scans")
    a.add_argument("--cooldown", type=int, default=60, help="Minutes before re-alerting the same item")
    a.add_argument("--max-drop-risk", type=float, default=0.10,
                   help="Max share of last 48h the price sat below your buy price (0.10 = 10%%)")
    a.add_argument("--max-downtrend", type=float, default=3.0, help="Max 24h price drop %% allowed")
    a.add_argument("--max-spike", type=float, default=8.0, help="Max %% your buy can sit above the 7-day median")
    a.add_argument("--min-margin-held", type=float, default=0.60,
                   help="Min share of last 24h the after-tax margin was positive")
    a.add_argument("--no-risk-check", action="store_true", help="Alert without the price-history check")
    a.add_argument("--f2p", action="store_true", help="Free-to-play items only")
    a.add_argument("--once", action="store_true", help="Scan once and exit")
    a.add_argument("--ntfy", default=os.getenv("NTFY_TOPIC"), help="ntfy.sh topic for phone push alerts")
    a.add_argument("--discord", default=os.getenv("DISCORD_WEBHOOK"), help="Discord webhook URL")
    return a.parse_args()


def main():
    cfg = parse_args()
    print("Loading item list...")
    while True:
        try:
            mapping = load_mapping()
            break
        except requests.RequestException as e:
            print(f"Couldn't reach the price API ({e}). Retrying in 30s...")
            time.sleep(30)
    print(f"Tracking {len(mapping):,} items. Ctrl+C to stop.")
    history = load_history()

    while True:
        try:
            latest = fetch("latest")["data"]
            hourly = fetch("1h")["data"]
            rows = analyze(mapping, latest, hourly, cfg)
            print_table(rows, cfg)
            send_alerts(rows, cfg, history)
        except requests.RequestException as e:
            print(f"Network error: {e} (retrying next cycle)")
        if cfg.once:
            break
        time.sleep(cfg.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
