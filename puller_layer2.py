#!/usr/bin/env python3
"""
CoinGecko fiat-pair depth puller: USDT and USDC order books quoted in
EM fiat currencies (USDT/TRY, USDC/MXN, USDT/NGN, ...).

Purpose: measure the depth of the ACTUAL fiat off-ramp layer (selling
dollar stablecoins for local fiat on centralized/local exchanges), to
complement the tokenized local-currency stablecoin layer measured
earlier. Together they form the two-layer evidence structure:
  Layer 1: tokenized local-currency stablecoin pools (already pulled)
  Layer 2: dollar-stablecoin vs fiat order books (this script)

FREE, KEYLESS public API. Conservative pacing (~5 calls/min) with
429 backoff. Resumable: rerun to continue after interruption.

Note on size: tether has several thousand tickers (~40-70 pages),
usd-coin ~10-20 pages. At 12s per call expect roughly 10-20 minutes.
The script filters locally to fiat targets and writes only those rows.

Output: cg_data/cg_fiat_pairs_depth.csv
Columns match the earlier tickers pull, plus base_coin.

Usage:
  python coingecko_fiat_pairs.py
  python coingecko_fiat_pairs.py --key YOUR_DEMO_KEY --interval 2.2
"""

import argparse
import csv
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

BASE = "https://api.coingecko.com/api/v3"
OUTDIR = "cg_data"
OUTFILE = "cg_fiat_pairs_depth.csv"
MIN_INTERVAL = 12.0
MAX_RETRIES = 6
BACKOFF_BASE = 5.0
BACKOFF_CAP = 120.0

# Dollar stablecoins whose fiat-quoted order books we want.
BASE_COINS = ["tether", "usd-coin"]

# Fiat quote currencies to keep (ISO codes as CoinGecko reports targets).
EM_FIATS = ["NGN", "MXN", "BRL", "TRY", "ZAR", "IDR", "COP", "ARS", "KES",
            "PHP", "THB", "GHS", "PEN", "CLP", "VND", "PKR", "EGP", "UAH",
            "INR", "MYR"]
BENCH_FIATS = ["EUR", "SGD"]
KEEP_TARGETS = set(EM_FIATS + BENCH_FIATS)

HEADER = [
    "base_coin", "base", "target", "market_name", "market_identifier",
    "converted_volume_usd", "bid_ask_spread_pct", "cost_to_move_up_usd",
    "cost_to_move_down_usd", "trust_score", "is_stale", "is_anomaly",
    "last_traded_at", "snapshot_utc",
]


class CG:
    def __init__(self, key=None, interval=MIN_INTERVAL):
        self.s = requests.Session()
        self.h = {"accept": "application/json"}
        if key:
            self.h["x-cg-demo-api-key"] = key
        self.interval = interval
        self._last = 0.0
        self.audit_path = os.path.join(OUTDIR, "coingecko_fiat_raw.jsonl")

    def _audit(self, path, params, status, payload=None):
        record = {"requested_utc": datetime.now(timezone.utc).isoformat(),
                  "path": path, "params": params or {}, "status": status,
                  "payload": payload}
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get(self, path, params=None):
        for attempt in range(MAX_RETRIES):
            dt = time.time() - self._last
            if dt < self.interval:
                time.sleep(self.interval - dt)
            try:
                r = self.s.get(BASE + path, params=params, headers=self.h,
                               timeout=45)
            except requests.RequestException as e:
                wait = min(BACKOFF_BASE * 2 ** attempt, BACKOFF_CAP)
                print(f"    network error ({e.__class__.__name__}); "
                      f"retry in {wait:.0f}s")
                time.sleep(wait)
                self._last = time.time()
                continue
            self._last = time.time()
            if r.status_code == 200:
                try:
                    data = r.json()
                    self._audit(path, params, r.status_code, data)
                    return data
                except ValueError:
                    time.sleep(BACKOFF_BASE)
                    continue
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = float(ra) if (ra and ra.isdigit()) else min(
                    BACKOFF_BASE * 2 ** attempt, BACKOFF_CAP)
                wait = max(wait, 15.0)
                print(f"    429; sleeping {wait:.0f}s "
                      f"({attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                wait = min(BACKOFF_BASE * 2 ** attempt, BACKOFF_CAP)
                print(f"    server {r.status_code}; retry {wait:.0f}s")
                time.sleep(wait)
                continue
            print(f"    HTTP {r.status_code}: {r.text[:150]}")
            self._audit(path, params, r.status_code, {"body": r.text[:500]})
            return None
        print("    giving up on", path)
        return None


def done_pages(path):
    """Resume support: (coin -> max page already fully written)."""
    prog = {}
    marker = os.path.join(OUTDIR, "fiat_pairs_progress.txt")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            for line in f:
                coin, page = line.strip().split(",")
                prog[coin] = max(prog.get(coin, 0), int(page))
    return prog, marker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=None, help="optional free Demo key")
    ap.add_argument("--interval", type=float, default=None)
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    interval = args.interval if args.interval is not None else (
        2.2 if args.key else MIN_INTERVAL)
    cg = CG(args.key, interval)
    out = os.path.join(OUTDIR, OUTFILE)
    prog, marker = done_pages(out)
    new_file = not os.path.exists(out)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fiat-pair depth puller. pacing={interval:.1f}s/call, "
          f"keeping targets: {sorted(KEEP_TARGETS)}")
    kept_total = 0
    with open(out, "a", newline="", encoding="utf-8") as f, \
         open(marker, "a", encoding="utf-8") as mk:
        w = csv.writer(f)
        if new_file:
            w.writerow(HEADER)
        for coin in BASE_COINS:
            start_page = prog.get(coin, 0) + 1
            print(f"[{coin}] starting at page {start_page}")
            page = start_page
            while True:
                data = cg.get(f"/coins/{urllib.parse.quote(coin)}/tickers", {
                    "depth": "true",
                    "order": "volume_desc",
                    "page": page,
                })
                if not data or not data.get("tickers"):
                    print(f"[{coin}] no more tickers at page {page}; done")
                    break
                kept = 0
                for t in data["tickers"]:
                    tgt = str(t.get("target", "")).upper()
                    if tgt not in KEEP_TARGETS:
                        continue
                    mktd = t.get("market") or {}
                    cv = t.get("converted_volume") or {}
                    w.writerow([
                        coin, t.get("base"), tgt,
                        mktd.get("name"), mktd.get("identifier"),
                        cv.get("usd"),
                        t.get("bid_ask_spread_percentage"),
                        t.get("cost_to_move_up_usd"),
                        t.get("cost_to_move_down_usd"),
                        t.get("trust_score"),
                        t.get("is_stale"), t.get("is_anomaly"),
                        t.get("last_traded_at"),
                        stamp,
                    ])
                    kept += 1
                kept_total += kept
                f.flush()
                mk.write(f"{coin},{page}\n")
                mk.flush()
                print(f"[{coin}] page {page}: kept {kept} fiat-pair rows "
                      f"(cumulative {kept_total})")
                if len(data["tickers"]) < 100:
                    break
                page += 1
                if page > 120:
                    print(f"[{coin}] page cap reached; stopping")
                    break

    print(f"\nDone. {kept_total} fiat-pair rows this run -> {out}")
    print("Hand cg_fiat_pairs_depth.csv back for the two-layer table.")


if __name__ == "__main__":
    main()
