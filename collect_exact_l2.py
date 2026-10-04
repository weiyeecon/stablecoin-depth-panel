#!/usr/bin/env python3
"""Collect native CEX L2 snapshots and compute executable off-ramp curves.

Public-book adapters include BtcTurk, Mercado Bitcoin, Indodax, VALR,
Bitkub, Bitso, and Binance (disabled by default after HTTP 451). All configured
pairs have the dollar stablecoin as base, so an off-ramp sale consumes bids.
Raw JSON, request metadata, hashes, threshold depth, and fixed-flow execution
curves are saved for every attempt. No trade is submitted and no key is used.
"""
import argparse
import hashlib
import json
import os
import math
import re
import uuid
from pathlib import Path
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BPS_GRID = [10, 50, 100, 200]
REQUIRED_CORRIDORS = {"TRY", "BRL", "IDR", "ZAR", "THB"}
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
    if venue == "btcturk":
        return "https://api.btcturk.com/api/v2/orderbook", {"pairSymbol": pair,
                                                          "limit": limit}
    if venue == "mercadobitcoin":
        return f"https://api.mercadobitcoin.net/api/v4/{pair}/orderbook", {"limit": limit}
    if venue == "indodax":
        return f"https://indodax.com/api/depth/{pair}", {}
    if venue == "valr":
        return f"https://api.valr.com/v1/public/{pair}/orderbook/full", {}
    if venue == "bitkub":
        return "https://api.bitkub.com/api/v3/market/depth", {"sym": pair,
                                                                "lmt": limit}
    if venue == "bitso":
        return "https://bitso.com/api/v3/order_book/", {"book": pair,
                                                         "aggregate": "true"}
    raise ValueError(f"unsupported venue {venue}")


def _levels(values):
    return [(float(item[0]), float(item[1])) for item in values]


def normalize_book(venue, payload):
    """Return bids and asks as (price quote/base, quantity base)."""
    if venue == "binance":
        return _levels(payload["bids"]), _levels(payload["asks"])
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    if venue == "btcturk":
        if payload.get("success") is not True:
            raise ValueError(f"BtcTurk API error: {payload.get('message')}")
        return _levels(payload["data"]["bids"]), _levels(payload["data"]["asks"])
    if venue == "mercadobitcoin":
        return _levels(payload["bids"]), _levels(payload["asks"])
    if venue == "indodax":
        return _levels(payload["buy"]), _levels(payload["sell"])
    if venue == "valr":
        bids = [(float(x["price"]), float(x.get("quantity", x.get("amount"))))
                for x in payload["Bids"]]
        asks = [(float(x["price"]), float(x.get("quantity", x.get("amount"))))
                for x in payload["Asks"]]
        return bids, asks
    if venue == "bitkub":
        if payload.get("error") != 0:
            raise ValueError(f"Bitkub API error: {payload.get('error')}")
        result = payload["result"]
        return _levels(result["bids"]), _levels(result["asks"])
    if venue == "bitso":
        if payload.get("success") is False:
            raise ValueError(f"Bitso API error: {payload.get('error')}")
        result = payload["payload"]
        bids = [(float(x["price"]), float(x["amount"])) for x in result["bids"]]
        asks = [(float(x["price"]), float(x["amount"])) for x in result["asks"]]
        return bids, asks
    raise ValueError(f"unsupported venue {venue}")


def validate_book(bids, asks):
    if any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0
           for p, q in bids + asks):
        raise ValueError("non-finite, non-positive price or negative quantity")
    # Zero-volume levels are harmless; all other malformed values fail closed.
    bids = sorted([(p, q) for p, q in bids if q > 0], reverse=True)
    asks = sorted([(p, q) for p, q in asks if q > 0])
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
                     "executable_stablecoin_units": base_notional,
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
    return {"requested_stablecoin_units": flow_usd,
            "filled_stablecoin_units": filled,
            "requested_stablecoin_usd": flow_usd,
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


class CollectionError(RuntimeError):
    def __init__(self, message, http_status=None, error_kind="request_error"):
        super().__init__(message)
        self.http_status = http_status
        self.error_kind = error_kind


def request_json(url, params, timeout=30, retries=3):
    """Retry only transient failures. Never reroute an access restriction."""
    import requests
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=timeout,
                                    headers={"accept": "application/json",
                                             "user-agent": "academic-depth-study/3.0"})
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt + 1 == retries:
                raise CollectionError(str(exc)) from exc
            time.sleep(2 ** (attempt + 1))
            continue
        if response.status_code == 200:
            try:
                return response.json(), response
            except ValueError as exc:
                raise CollectionError("HTTP 200 was not valid JSON", 200,
                                      "invalid_response") from exc
        status = response.status_code
        if status in (429, 500, 502, 503, 504) and attempt + 1 < retries:
            delay = 2 ** (attempt + 1)
            retry_after = response.headers.get("Retry-After", "")
            if str(retry_after).isdigit():
                delay = max(delay, min(int(retry_after), 120))
            time.sleep(delay)
            continue
        kind = "access_restricted" if status in (403, 451) else "http_error"
        raise CollectionError(f"HTTP {status}: {response.text[:300]}", status, kind)
    raise CollectionError("request did not return a response")


def exchange_timestamp(venue, payload):
    """Only documented snapshot clocks; request receipt is recorded separately."""
    value = None
    if venue == "btcturk":
        value = payload.get("data", {}).get("timestamp")
        if value is not None:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
    elif venue == "mercadobitcoin":
        value = payload.get("timestamp")
        if value is not None:
            # API documents a numeric timestamp without specifying its unit.
            value = float(value)
            return datetime.fromtimestamp(value / 1000 if value >= 1e12 else value,
                                          timezone.utc).isoformat()
    elif venue == "bitso":
        value = payload.get("payload", {}).get("updated_at")
        if value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return None


def summarize_coverage(attempts, required_corridors=REQUIRED_CORRIDORS):
    active = [a for a in attempts if a["status"] != "disabled"]
    success = [a for a in active if a["status"] == "success"]
    observed = {a["corridor"] for a in success}
    missing = sorted(set(required_corridors) - observed)
    failed = [f"{a['venue']}:{a['pair']}" for a in active if a["status"] != "success"]
    return {"status": "complete" if active and not failed and not missing else "partial",
            "expected_sources": len(active), "successful_sources": len(success),
            "failed_sources": failed, "required_corridors": sorted(required_corridors),
            "observed_corridors": sorted(observed), "missing_corridors": missing,
            "disabled_sources": [f"{a['venue']}:{a['pair']}" for a in attempts
                                 if a["status"] == "disabled"],
            "scope": "configured active native markets only; not historical Binance coverage"}


def write_summary(manifest, summary_path):
    coverage = manifest["coverage"]
    lines = ["## Native L2 collection", "",
             f"Snapshot: {manifest['snapshot_id']}",
             f"Coverage: **{coverage['status']}**; {coverage['successful_sources']}/"
             f"{coverage['expected_sources']} active books received.",
             "Missing required corridors: " + (", ".join(coverage["missing_corridors"]) or "none"),
             "", "| Venue | Pair | Corridor | Status | Detail |",
             "|---|---|---|---|---|"]
    for attempt in manifest["attempts"]:
        detail = attempt.get("error", attempt.get("note", ""))
        detail = str(detail).replace("|", "/").replace("\n", " ")[:200]
        lines.append(f"| {attempt['venue']} | {attempt['pair']} | {attempt['corridor']} | "
                     f"{attempt['status']} | {detail} |")
    lines += ["", "Alternative venues are separate markets, not Binance backfills.",
              "Native L2 outputs are kept separate from CoinGecko proxy panels.",
              "Quantities are stablecoin tokens; legacy USD aliases assume a unit peg.",
              "A successful request does not guarantee full depth: inspect depth_is_lower_bound.", ""]
    Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def collect(config_path, output_root, day=None, summary_path=None):
    now = datetime.now(timezone.utc)
    actual_day = now.strftime("%Y-%m-%d")
    if day and day != actual_day:
        raise ValueError("live order books cannot be backdated; --date must be today's UTC date")
    day = actual_day
    snapshot_id = now.strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
    output = Path(output_root) / day / "runs" / snapshot_id
    output.mkdir(parents=True, exist_ok=False)
    config = pd.read_csv(config_path, keep_default_na=False)
    threshold_rows, curve_rows, attempts = [], [], []
    config.to_csv(output / "config.csv", index=False)
    for _, row in config.iterrows():
        venue, pair = str(row["venue"]).lower(), str(row["pair"])
        attempt = {"venue": venue, "pair": pair, "corridor": str(row["corridor"]),
                   "note": str(row.get("note", "")),
                   "collected_utc": datetime.now(timezone.utc).isoformat()}
        if str(row["enabled"]).lower() != "true":
            attempts.append({**attempt, "status": "disabled"})
            continue
        try:
            if str(row["stablecoin_side"]) != "base":
                raise ValueError("only stablecoin-base books are supported")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", venue + pair):
                raise ValueError("unsafe venue/pair identifier")
            url, params = endpoint({**row.to_dict(), "venue": venue})
            attempt.update(source_url=url, source_params=params)
            payload, response = request_json(url, params)
            attempt.update(received_utc=datetime.now(timezone.utc).isoformat(),
                           http_status=response.status_code)
            raw_path = output / f"{venue}_{pair.lower()}_raw.json"
            with raw_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            attempt.update(raw_file=raw_path.name, raw_sha256=sha256(raw_path))
            bids, asks = validate_book(*normalize_book(venue, payload))
            timestamp = exchange_timestamp(venue, payload)
            if timestamp:
                age = (datetime.fromisoformat(attempt["received_utc"]) -
                       datetime.fromisoformat(timestamp)).total_seconds()
                if age > 900 or age < -60:
                    raise ValueError(f"exchange snapshot clock outside freshness window: {age:.1f}s")
            meta = {"date": day, "snapshot_id": snapshot_id,
                    "collected_utc": attempt["collected_utc"],
                    "received_utc": attempt["received_utc"],
                    "exchange_timestamp_utc": timestamp,
                    "exchange_timestamp_status": "provided" if timestamp else "unavailable",
                    "exchange_timestamp_encoding": ("epoch_unit_inferred_by_magnitude"
                        if venue == "mercadobitcoin" and timestamp else
                        "documented_exchange_clock" if timestamp else "unavailable"),
                    "venue": venue, "pair": pair, "corridor": row["corridor"],
                    "stablecoin": row["stablecoin"], "base_asset": row["stablecoin"],
                    "quote_asset": row["corridor"], "source_type": "native_l2",
                    "price_unit": f"{row['corridor']} per {row['stablecoin']}",
                    "quantity_unit": row["stablecoin"],
                    "usd_conversion_assumption": "1 stablecoin token = 1 USD; no FX conversion",
                    "source_url": url, "source_params_json": json.dumps(params, sort_keys=True),
                    "off_ramp_action": "sell_stablecoin_base_hit_bids",
                    "raw_file": raw_path.name, "raw_sha256": attempt["raw_sha256"]}
            threshold_rows.extend(threshold_depth_rows(bids, asks, meta))
            curve_rows.extend(execution_rows(bids, asks, meta))
            attempt.update(status="success", bid_levels=len(bids), ask_levels=len(asks),
                           exchange_timestamp_utc=timestamp)
        except Exception as exc:
            attempt.update(status="failed", error=str(exc)[:500],
                           error_kind=getattr(exc, "error_kind", "invalid_book"))
            if getattr(exc, "http_status", None) is not None:
                attempt["http_status"] = exc.http_status
        attempts.append(attempt)

    depth_path, curve_path = output / "threshold_depth.csv", output / "execution_curves.csv"
    pd.DataFrame(threshold_rows).to_csv(depth_path, index=False)
    pd.DataFrame(curve_rows).to_csv(curve_path, index=False)
    manifest = {"schema_version": 2, "snapshot_id": snapshot_id, "date": day,
                "source_type": "native_l2", "historical_backfill": False,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "config_sha256": sha256(config_path), "code_sha256": sha256(__file__),
                "config_archive_sha256": sha256(output / "config.csv"),
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "github_sha": os.environ.get("GITHUB_SHA"),
                "attempts": attempts, "coverage": summarize_coverage(attempts),
                "outputs": {path.name: sha256(path) for path in (depth_path, curve_path)}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"snapshot_path={output.resolve()}\n")
    if summary_path:
        write_summary(manifest, summary_path)
    coverage = manifest["coverage"]
    print(f"exact L2 {day}: {coverage['successful_sources']}/{coverage['expected_sources']} "
          f"active books; {coverage['status']} -> {output}")
    for attempt in attempts:
        print(f"  {attempt['venue']} {attempt['pair']}: {attempt['status']} "
              f"{attempt.get('error', '')}")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.path.join(HERE, "exact_l2_config.csv"))
    parser.add_argument("--date", default=None, help="UTC today only; no historical backdating")
    parser.add_argument("--force", action="store_true", help="legacy option; all attempts are append-only")
    parser.add_argument("--output-root", default=os.path.join(HERE, "data", "exact_l2"))
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()
    manifest = collect(args.config, args.output_root, args.date, args.summary)
    if manifest["coverage"]["status"] != "complete":
        raise SystemExit("native L2 coverage incomplete; partial data saved; inspect manifest.json")


if __name__ == "__main__":
    main()
