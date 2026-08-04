#!/usr/bin/env python3
"""
CoinGecko data puller for EM stablecoin corridor evidence.

Targets the FREE, KEYLESS public API (https://api.coingecko.com/api/v3).
No account or API key required. Written to respect the free-tier rate
limits: conservative pacing (~5 calls/min by default) plus exponential
backoff with Retry-After handling on HTTP 429.

Free-tier constraints handled here:
  * No API key: base URL api.coingecko.com, no auth header.
  * market_chart history is capped at the trailing 365 days on the free
    tier, and the `interval=daily` parameter is NOT allowed (granularity
    is automatic; days>90 already returns daily points). We therefore
    request days=365 with no interval. The 2023 USDC depeg (older than
    365 days) is NOT retrievable on the free tier.
  * tickers are paginated at 100 per page; we page until empty.

Outputs (written incrementally, resumable):
  cg_markets.csv         all stablecoins with USD market cap (+ our peg tag)
  cg_tickers_depth.csv   per-ticker 2% depth, spread, CEX/DEX venue
  cg_market_chart.csv    trailing-365d daily price / mcap / volume (USD)

Requires: requests  (pip install requests)

Usage:
  python coingecko_puller.py                 # run everything
  python coingecko_puller.py --skip-chart    # markets + depth only (faster)
  python coingecko_puller.py --key DEMO_KEY  # optional free Demo key (30/min)

If you later sign up for a free Demo key, pass --key; the script will use
the Demo base URL/header and you can lower MIN_INTERVAL to ~2.2s.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
PUBLIC_BASE = "https://api.coingecko.com/api/v3"
DEMO_BASE = "https://api.coingecko.com/api/v3"          # Demo key uses same host
PRO_BASE = "https://pro-api.coingecko.com/api/v3"

OUTDIR = "cg_data"
MIN_INTERVAL = 12.0     # seconds between calls; keyless free tier is ~5/min.
                        # With a Demo key you can set this to ~2.2 (30/min).
MAX_RETRIES = 6
BACKOFF_BASE = 5.0      # seconds; grows as BACKOFF_BASE * 2**attempt (capped)
BACKOFF_CAP = 120.0
CHART_DAYS = 365        # free-tier max lookback

# EM fiat-peg classification. Symbols are matched first (uppercased),
# then a name-substring fallback. USD-pegged coins never match, so they
# are dropped automatically. SGD and EUR are kept as advanced-market
# benchmarks. Extend freely.
PEG_TICKERS = {
    "EUR": ["EURC", "EURT", "EURCV", "EURS", "AGEUR", "EURE", "EURI", "EUROE",
            "EURO3", "EURR", "EURD", "CEUR", "AEUR", "EURA", "VEUR", "SEUR",
            "EURQ", "EURW", "EURM", "EEUR", "IBEUR", "PAR"],
    "SGD": ["XSGD"],
    "BRL": ["BRZ", "BRLA", "CREAL", "BRL1"],
    "TRY": ["TRYB"],
    "MXN": ["MXNE", "MXNT", "MXNB", "WMXN"],
    "IDR": ["IDRX", "IDRT", "XIDR"],
    "ZAR": ["ZARP", "ZARM"],
    "NGN": ["CNGN", "NGNC", "NGNM"],
    "PHP": ["PHPC", "PHT", "PHPT", "PHPM"],
    "ARS": ["NARS", "WARS", "ARST", "ARSX"],
    "COP": ["COPM", "WCOP"],
    "KES": ["KESM"],
    "GHS": ["GHSM"],
    "THB": ["THBT", "XTHB"],
}
NAME_HINTS = {
    "EUR": ["euro"],
    "BRL": ["real", "brazil"],
    "TRY": ["lira", "turk"],
    "MXN": ["mexican", "mexico"],
    "IDR": ["rupiah", "indones"],
    "ZAR": ["rand", "south afric"],
    "NGN": ["naira", "niger"],
    "PHP": ["philippine"],
    "ARS": ["argentin"],
    "COP": ["colombia"],
    "SGD": ["singapore"],
    "KES": ["kenya", "shilling"],
    "GHS": ["ghana", "cedi"],
    "THB": ["baht", "thai"],
}
# Manually force-include coin ids the classifier might miss (optional).
EXTRA_IDS = []   # e.g. ["some-coin-id"]

SYM_TO_CCY = {sym: ccy for ccy, syms in PEG_TICKERS.items() for sym in syms}


# ----------------------------------------------------------------------
# Rate-limited HTTP session
# ----------------------------------------------------------------------
class CGSession:
    def __init__(self, api_key=None, min_interval=MIN_INTERVAL):
        self.s = requests.Session()
        self.min_interval = min_interval
        self._last = 0.0
        self.base = PUBLIC_BASE
        self.headers = {"accept": "application/json"}
        if api_key:
            # Demo key by default; switch base to PRO_BASE + change header
            # name to x-cg-pro-api-key if you actually hold a Pro key.
            self.base = DEMO_BASE
            self.headers["x-cg-demo-api-key"] = api_key
        self.audit_path = os.path.join(OUTDIR, "coingecko_token_raw.jsonl")

    def _audit(self, path, params, status, payload=None):
        record = {"requested_utc": now_utc(), "path": path,
                  "params": params or {}, "status": status, "payload": payload}
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _pace(self):
        dt = time.time() - self._last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)

    def get(self, path, params=None):
        url = self.base + path
        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                r = self.s.get(url, params=params, headers=self.headers, timeout=45)
            except requests.RequestException as e:
                wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
                print(f"    network error ({e.__class__.__name__}); retry in {wait:.0f}s")
                time.sleep(wait)
                self._last = time.time()
                continue
            self._last = time.time()

            if r.status_code == 200:
                try:
                    data = r.json()
                    self._audit(path, params, r.status_code, data)
                    return data
                except json.JSONDecodeError:
                    print("    got 200 but non-JSON body; retrying")
                    time.sleep(BACKOFF_BASE)
                    continue

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else min(
                    BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
                wait = max(wait, 15.0)
                print(f"    429 rate limited; sleeping {wait:.0f}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue

            if r.status_code in (500, 502, 503, 504):
                wait = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
                print(f"    server {r.status_code}; retry in {wait:.0f}s")
                time.sleep(wait)
                continue

            if r.status_code == 404:
                print(f"    404 not found: {path}")
                return None

            if r.status_code == 401:
                print("    401 unauthorized: a parameter may be gated on the "
                      "free tier, or the key is invalid. Body:", r.text[:200])
                return None

            print(f"    unexpected {r.status_code}: {r.text[:200]}")
            self._audit(path, params, r.status_code, {"body": r.text[:500]})
            time.sleep(BACKOFF_BASE)
        print(f"    giving up on {path} after {MAX_RETRIES} attempts")
        return None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_peg(symbol, name):
    sym = (symbol or "").upper()
    if sym in SYM_TO_CCY:
        return SYM_TO_CCY[sym]
    nm = (name or "").lower()
    for ccy, hints in NAME_HINTS.items():
        if any(h in nm for h in hints):
            return ccy
    return None


def load_done_ids(path, id_col):
    """For resumability: return set of coin ids already written."""
    done = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row[id_col])
    return done


# ----------------------------------------------------------------------
# Step 0 + 2: stablecoin markets (USD market cap) + peg classification
# ----------------------------------------------------------------------
def fetch_stablecoin_markets(cg):
    print("[1/3] Fetching stablecoin markets (USD market cap) ...")
    rows = []
    page = 1
    while True:
        data = cg.get("/coins/markets", {
            "vs_currency": "usd",
            "category": "stablecoins",
            "per_page": 250,
            "page": page,
            "price_change_percentage": "24h",
        })
        if not data:
            break
        rows.extend(data)
        print(f"    page {page}: {len(data)} coins (cumulative {len(rows)})")
        if len(data) < 250:
            break
        page += 1
        if page > 10:
            break

    out = os.path.join(OUTDIR, "cg_markets.csv")
    em_ids = []
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "symbol", "name", "peg_ccy", "current_price",
                    "market_cap", "total_volume", "circulating_supply",
                    "price_change_percentage_24h", "last_updated"])
        for c in rows:
            peg = classify_peg(c.get("symbol"), c.get("name"))
            if c.get("id") in EXTRA_IDS and not peg:
                peg = "MANUAL"
            if not peg:
                continue
            em_ids.append((c["id"], peg, c.get("symbol")))
            w.writerow([
                c.get("id"), c.get("symbol"), c.get("name"), peg,
                c.get("current_price"), c.get("market_cap"),
                c.get("total_volume"), c.get("circulating_supply"),
                c.get("price_change_percentage_24h_in_currency"),
                c.get("last_updated"),
            ])
    # de-dup, keep order
    seen, uniq = set(), []
    for cid, peg, sym in em_ids:
        if cid not in seen:
            seen.add(cid)
            uniq.append((cid, peg, sym))
    print(f"    total stablecoins scanned: {len(rows)}")
    print(f"    EM/benchmark fiat-peg coins kept: {len(uniq)}")
    by_ccy = {}
    for cid, peg, sym in uniq:
        by_ccy.setdefault(peg, []).append(sym)
    for ccy in sorted(by_ccy):
        print(f"      {ccy}: {by_ccy[ccy]}")
    print(f"    -> {out}")
    return uniq


# ----------------------------------------------------------------------
# Step 1: per-coin tickers with 2% depth
# ----------------------------------------------------------------------
TICKER_HEADER = [
    "coin_id", "peg_ccy", "base", "target", "market_name", "market_identifier",
    "converted_volume_usd", "bid_ask_spread_pct", "cost_to_move_up_usd",
    "cost_to_move_down_usd", "trust_score", "is_stale", "is_anomaly",
    "last_traded_at", "target_coin_id", "snapshot_utc",
]


def fetch_tickers(cg, coins):
    print("[2/3] Fetching tickers with 2% depth (CEX + DEX) ...")
    out = os.path.join(OUTDIR, "cg_tickers_depth.csv")
    done = load_done_ids(out, "coin_id")
    new_file = not os.path.exists(out)
    stamp = now_utc()

    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(TICKER_HEADER)
        for i, (cid, peg, sym) in enumerate(coins, 1):
            if cid in done:
                print(f"    ({i}/{len(coins)}) {sym} [{peg}] already done, skip")
                continue
            print(f"    ({i}/{len(coins)}) {sym} [{peg}] id={cid}")
            page = 1
            n_written = 0
            while True:
                data = cg.get(f"/coins/{urllib.parse.quote(cid)}/tickers", {
                    "depth": "true",
                    "dex_pair_format": "symbol",
                    "order": "volume_desc",
                    "page": page,
                })
                if not data or "tickers" not in data:
                    break
                tickers = data["tickers"]
                if not tickers:
                    break
                for t in tickers:
                    mkt = t.get("market") or {}
                    cv = t.get("converted_volume") or {}
                    w.writerow([
                        t.get("coin_id") or cid, peg,
                        t.get("base"), t.get("target"),
                        mkt.get("name"), mkt.get("identifier"),
                        cv.get("usd"),
                        t.get("bid_ask_spread_percentage"),
                        t.get("cost_to_move_up_usd"),
                        t.get("cost_to_move_down_usd"),
                        t.get("trust_score"),
                        t.get("is_stale"), t.get("is_anomaly"),
                        t.get("last_traded_at"),
                        t.get("target_coin_id"),
                        stamp,
                    ])
                    n_written += 1
                if len(tickers) < 100:
                    break
                page += 1
                if page > 10:
                    break
            f.flush()
            print(f"        wrote {n_written} tickers")
    print(f"    -> {out}")


# ----------------------------------------------------------------------
# Step 3: trailing-365d daily market chart (USD)
# ----------------------------------------------------------------------
def fetch_market_chart(cg, coins):
    print("[3/3] Fetching trailing-365d daily price/mcap/volume (USD) ...")
    out = os.path.join(OUTDIR, "cg_market_chart.csv")
    done = load_done_ids(out, "id")
    new_file = not os.path.exists(out)

    with open(out, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["id", "peg_ccy", "date", "price_usd",
                        "market_cap_usd", "total_volume_usd"])
        for i, (cid, peg, sym) in enumerate(coins, 1):
            if cid in done:
                print(f"    ({i}/{len(coins)}) {sym} already done, skip")
                continue
            print(f"    ({i}/{len(coins)}) {sym} [{peg}] id={cid}")
            data = cg.get(f"/coins/{urllib.parse.quote(cid)}/market_chart", {
                "vs_currency": "usd",
                "days": CHART_DAYS,       # no `interval` on free tier
            })
            if not data:
                continue
            prices = {int(p[0]): p[1] for p in data.get("prices", [])}
            mcaps = {int(p[0]): p[1] for p in data.get("market_caps", [])}
            vols = {int(p[0]): p[1] for p in data.get("total_volumes", [])}
            for ts in sorted(prices):
                d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                w.writerow([cid, peg, d, prices.get(ts),
                            mcaps.get(ts), vols.get(ts)])
            f.flush()
            print(f"        wrote {len(prices)} daily points")
    print(f"    -> {out}")


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=None,
                    help="optional free Demo API key (raises limit to ~30/min)")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between calls (override; keyless default 12)")
    ap.add_argument("--skip-chart", action="store_true",
                    help="skip the historical market_chart step")
    ap.add_argument("--only-markets", action="store_true",
                    help="only fetch the stablecoin markets list, then stop")
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    interval = args.interval if args.interval is not None else (
        2.2 if args.key else MIN_INTERVAL)
    cg = CGSession(api_key=args.key, min_interval=interval)

    print(f"CoinGecko puller starting. base={cg.base} "
          f"key={'yes' if args.key else 'no (keyless free tier)'} "
          f"pacing={interval:.1f}s/call")
    print(f"Output dir: {os.path.abspath(OUTDIR)}\n")

    coins = fetch_stablecoin_markets(cg)
    if not coins:
        print("No coins classified. Check PEG_TICKERS / NAME_HINTS, or the "
              "stablecoins category response. Aborting.")
        sys.exit(1)
    if args.only_markets:
        return

    est = len(coins) * (1 if args.skip_chart else 2) * interval / 60.0
    print(f"\nAbout to fetch {'tickers' if args.skip_chart else 'tickers + chart'} "
          f"for {len(coins)} coins. Rough time estimate: ~{est:.0f} min "
          f"(the script is resumable; safe to Ctrl-C and rerun).\n")

    fetch_tickers(cg, coins)
    if not args.skip_chart:
        fetch_market_chart(cg, coins)

    print("\nDone. Hand the three CSVs in cg_data/ back for cleaning:")
    print("  cg_markets.csv, cg_tickers_depth.csv, cg_market_chart.csv")


if __name__ == "__main__":
    main()
