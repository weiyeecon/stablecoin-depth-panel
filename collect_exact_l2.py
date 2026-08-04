#!/usr/bin/env python3
"""Collect native CEX L2 snapshots and compute executable off-ramp curves.

Supported public-book adapters: Binance, VALR, Bitkub, and Bitso. All configured
pairs have the dollar stablecoin as base, so an off-ramp sale consumes bids.
Raw JSON, request metadata, hashes, threshold depth, and fixed-flow execution
curves are saved for every attempt. No trade is submitted and no key is used.
"""
import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BPS_GRID = [10, 50, 100, 200]
FLOW_GRID_USD = [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def endpoint(row):
    venue, pair, limit = row["venue"], row["pair"], int(row["limit"])
    if venue == "binance":
        return "https://api.binance.com/api/v3/depth", {"symbol": pair,
                                                          "limit": limit}
    if venue == "valr":
        return f"https://api.valr.com/v1/public/{pair}/orderbook/full", {}
    if venue == "bitkub":
        return "https://api.bitkub.com/api/v3/market/depth", {"sym": pair,
                                                                "lmt": limit}
    if venue == "bitso":
        return "https://api.bitso.com/v3/order_book/", {"book": pair,
                                                         "aggregate": "false"}
    raise ValueError(f"unsupported venue {venue}")


def _levels(values):
    return [(float(item[0]), float(item[1])) for item in values]


def normalize_book(venue, payload):
    """Return bids and asks as (price quote/base, quantity base)."""
    if venue == "binance":
        return _levels(payload["bids"]), _levels(payload["asks"])
    if venue == "valr":
        bids = [(float(x["price"]), float(x.get("quantity", x.get("amount"))))
                for x in payload["Bids"]]
        asks = [(float(x["price"]), float(x.get("quantity", x.get("amount"))))
                for x in payload["Asks"]]
        return bids, asks
    if venue == "bitkub":
        result = payload["result"]
        return _levels(result["bids"]), _levels(result["asks"])
    if venue == "bitso":
        result = payload["payload"]
        bids = [(float(x["price"]), float(x["amount"])) for x in result["bids"]]
        asks = [(float(x["price"]), float(x["amount"])) for x in result["asks"]]
        return bids, asks
    raise ValueError(f"unsupported venue {venue}")


def validate_book(bids, asks):
    bids = sorted([(p, q) for p, q in bids if p > 0 and q > 0], reverse=True)
    asks = sorted([(p, q) for p, q in asks if p > 0 and q > 0])
    if not bids or not asks:
        raise ValueError("empty bid or ask side")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("crossed or invalid order book")
    return bids, asks


def threshold_depth_rows(bids, asks, meta):
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    rows = []
    worst_bid = bids[-1][0]
    for bps in BPS_GRID:
        floor = mid * (1 - bps / 10_000)
        eligible = [(price, qty) for price, qty in bids if price >= floor]
        base_notional = sum(qty for _, qty in eligible)
        quote_proceeds = sum(price * qty for price, qty in eligible)
        rows.append({**meta, "threshold_bps": bps, "reference_mid": mid,
                     "best_bid": best_bid, "best_ask": best_ask,
                     "executable_stablecoin_usd": base_notional,
                     "quote_proceeds": quote_proceeds,
                     "book_covers_threshold": bool(worst_bid <= floor),
                     "depth_is_lower_bound": bool(worst_bid > floor),
                     "levels_used": len(eligible)})
    return rows


def execute_sale(bids, flow_usd, mid):
    remaining = float(flow_usd)
    filled = quote = 0.0
    terminal_price = np.nan
    levels = 0
    for price, quantity in bids:
        take = min(quantity, remaining)
        if take <= 0:
            continue
        filled += take
        quote += price * take
        remaining -= take
        terminal_price = price
        levels += 1
        if remaining <= 1e-12:
            break
    vwap = quote / filled if filled else np.nan
    return {"requested_stablecoin_usd": flow_usd,
            "filled_stablecoin_usd": filled,
            "fill_rate": filled / flow_usd if flow_usd else np.nan,
            "quote_proceeds": quote, "vwap_quote_per_stablecoin": vwap,
            "average_slippage_bps": (1 - vwap / mid) * 10_000 if filled else np.nan,
            "terminal_impact_bps": ((1 - terminal_price / mid) * 10_000
                                    if filled else np.nan),
            "levels_used": levels, "book_exhausted": remaining > 1e-12}


def execution_rows(bids, asks, meta):
    mid = (bids[0][0] + asks[0][0]) / 2
    return [{**meta, **execute_sale(bids, flow, mid)} for flow in FLOW_GRID_USD]


def request_json(url, params, timeout=45, retries=4):
    import requests
    delay = 2.0
    for attempt in range(retries):
        response = requests.get(url, params=params, timeout=timeout,
                                headers={"accept": "application/json",
                                         "user-agent": "academic-depth-study/2.0"})
        if response.status_code == 200:
            return response.json(), response
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(delay)
            delay *= 2
            continue
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    raise RuntimeError(f"request failed after {retries} attempts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(HERE, "exact_l2_config.csv"))
    parser.add_argument("--date", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = os.path.join(HERE, "data", "exact_l2", day)
    manifest_path = os.path.join(output, "manifest.json")
    if os.path.exists(manifest_path) and not args.force:
        print(f"exact L2 snapshot {day} exists; use --force to replace")
        return
    os.makedirs(output, exist_ok=True)
    config = pd.read_csv(args.config)
    config = config[config["enabled"].astype(str).str.lower().eq("true")]
    threshold_rows, curve_rows, attempts = [], [], []

    for _, row in config.iterrows():
        venue, pair = str(row["venue"]).lower(), str(row["pair"])
        raw_path = os.path.join(output, f"{venue}_{pair.lower()}_raw.json")
        started = datetime.now(timezone.utc).isoformat()
        try:
            url, params = endpoint(row)
            payload, response = request_json(url, params)
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            bids, asks = validate_book(*normalize_book(venue, payload))
            meta = {"date": day, "collected_utc": started, "venue": venue,
                    "pair": pair, "corridor": row["corridor"],
                    "stablecoin": row["stablecoin"],
                    "off_ramp_action": "sell_stablecoin_base_hit_bids",
                    "raw_file": os.path.basename(raw_path),
                    "raw_sha256": sha256(raw_path)}
            threshold_rows.extend(threshold_depth_rows(bids, asks, meta))
            curve_rows.extend(execution_rows(bids, asks, meta))
            attempts.append({"venue": venue, "pair": pair, "status": "success",
                             "http_status": response.status_code,
                             "raw_file": os.path.basename(raw_path),
                             "raw_sha256": sha256(raw_path)})
        except Exception as exc:
            attempts.append({"venue": venue, "pair": pair, "status": "failed",
                             "error": str(exc)[:500]})

    depth_path = os.path.join(output, "threshold_depth.csv")
    curve_path = os.path.join(output, "execution_curves.csv")
    pd.DataFrame(threshold_rows).to_csv(depth_path, index=False)
    pd.DataFrame(curve_rows).to_csv(curve_path, index=False)
    manifest = {"date": day, "created_utc": datetime.now(timezone.utc).isoformat(),
                "config_sha256": sha256(args.config), "code_sha256": sha256(__file__),
                "attempts": attempts,
                "outputs": {os.path.basename(path): sha256(path)
                            for path in (depth_path, curve_path)}}
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    succeeded = sum(a["status"] == "success" for a in attempts)
    print(f"exact L2 {day}: {succeeded}/{len(attempts)} books collected -> {output}")
    if succeeded != len(attempts):
        raise SystemExit("one or more exact-L2 books failed; inspect manifest.json")


if __name__ == "__main__":
    main()
