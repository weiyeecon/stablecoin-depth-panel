#!/usr/bin/env python3
"""Compute exact DEX execution curves from archived pool-state snapshots.

Two pool types are supported:

* ``constant_product``: reserve-based x*y=k pools;
* ``uniswap_v3``: current sqrtPriceX96, active liquidity, and every initialized
  tick crossed by the requested flow grid.

The script never infers reserves from TVL. Pool snapshots are explicit inputs
and should be archived with block number, chain id, pool address, and source.
"""
import argparse
from decimal import Decimal, getcontext
import hashlib
import json
import os

import pandas as pd

getcontext().prec = 70
Q96 = Decimal(2) ** 96
DEFAULT_FLOWS = [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def constant_product_quote(pool, flow_usd):
    x = Decimal(str(pool["reserve_stablecoin"]))
    y = Decimal(str(pool["reserve_local_token"]))
    amount = Decimal(str(flow_usd))
    fee = Decimal(str(pool.get("fee_bps", 30))) / Decimal(10_000)
    effective = amount * (Decimal(1) - fee)
    out = y * effective / (x + effective)
    spot = y / x
    execution = out / amount
    x_after, y_after = x + effective, y - out
    terminal = y_after / x_after
    return {"flow_usd": float(amount), "local_token_out": float(out),
            "effective_local_per_usd": float(execution),
            "average_slippage_bps": float((Decimal(1) - execution / spot) * 10_000),
            "terminal_impact_bps": float((Decimal(1) - terminal / spot) * 10_000),
            "liquidity_exhausted": False}


def human_spot(sqrt_price, decimals0, decimals1, stablecoin_token):
    raw_p = sqrt_price * sqrt_price
    if stablecoin_token == 0:
        return raw_p * (Decimal(10) ** Decimal(decimals0 - decimals1))
    return (Decimal(1) / raw_p) * (Decimal(10) ** Decimal(decimals1 - decimals0))


def uniswap_v3_quote(pool, flow_usd):
    decimals0, decimals1 = int(pool["decimals0"]), int(pool["decimals1"])
    stablecoin_token = int(pool["stablecoin_token"])
    stable_decimals = decimals0 if stablecoin_token == 0 else decimals1
    local_decimals = decimals1 if stablecoin_token == 0 else decimals0
    gross_remaining = Decimal(str(flow_usd)) * (Decimal(10) ** stable_decimals)
    fee = Decimal(str(pool["fee_bps"])) / Decimal(10_000)
    sqrt_p = Decimal(str(pool["sqrt_price_x96"])) / Q96
    initial_sqrt = sqrt_p
    liquidity = Decimal(str(pool["liquidity"]))
    ticks = [{"sqrt": Decimal(str(t["sqrt_price_x96"])) / Q96,
              "liquidity_net": Decimal(str(t["liquidity_net"])),
              "tick": int(t["tick"])} for t in pool["ticks"]]
    zero_for_one = stablecoin_token == 0
    ticks = sorted([t for t in ticks if (t["sqrt"] < sqrt_p if zero_for_one
                                         else t["sqrt"] > sqrt_p)],
                   key=lambda t: t["sqrt"], reverse=zero_for_one)
    raw_out = Decimal(0)
    exhausted = False

    for boundary in ticks + [None]:
        if gross_remaining <= 0:
            break
        if liquidity <= 0:
            exhausted = True
            break
        if boundary is None:
            exhausted = True
            break
        target = boundary["sqrt"]
        if zero_for_one:
            net_needed = liquidity * (sqrt_p - target) / (sqrt_p * target)
            out_segment = liquidity * (sqrt_p - target)
        else:
            net_needed = liquidity * (target - sqrt_p)
            out_segment = liquidity * (target - sqrt_p) / (target * sqrt_p)
        gross_needed = net_needed / (Decimal(1) - fee)
        if gross_remaining < gross_needed:
            net_available = gross_remaining * (Decimal(1) - fee)
            if zero_for_one:
                new_sqrt = Decimal(1) / (Decimal(1) / sqrt_p +
                                          net_available / liquidity)
                raw_out += liquidity * (sqrt_p - new_sqrt)
            else:
                new_sqrt = sqrt_p + net_available / liquidity
                raw_out += liquidity * (new_sqrt - sqrt_p) / (new_sqrt * sqrt_p)
            sqrt_p = new_sqrt
            gross_remaining = Decimal(0)
            break
        gross_remaining -= gross_needed
        raw_out += out_segment
        sqrt_p = target
        # liquidityNet is defined for crossing from lower to higher price.
        liquidity += boundary["liquidity_net"] if not zero_for_one else -boundary["liquidity_net"]

    filled_raw = (Decimal(str(flow_usd)) * (Decimal(10) ** stable_decimals) -
                  gross_remaining)
    filled_human = filled_raw / (Decimal(10) ** stable_decimals)
    out_human = raw_out / (Decimal(10) ** local_decimals)
    initial_spot = human_spot(initial_sqrt, decimals0, decimals1, stablecoin_token)
    terminal_spot = human_spot(sqrt_p, decimals0, decimals1, stablecoin_token)
    execution = out_human / filled_human if filled_human else Decimal("NaN")
    return {"flow_usd": float(flow_usd), "filled_usd": float(filled_human),
            "fill_rate": float(filled_human / Decimal(str(flow_usd))),
            "local_token_out": float(out_human),
            "effective_local_per_usd": float(execution),
            "average_slippage_bps": float((Decimal(1) - execution / initial_spot) * 10_000),
            "terminal_impact_bps": float((Decimal(1) - terminal_spot / initial_spot) * 10_000),
            "liquidity_exhausted": bool(exhausted and gross_remaining > 0)}


def compute(pool, flows):
    quote = (constant_product_quote if pool["pool_type"] == "constant_product"
             else uniswap_v3_quote if pool["pool_type"] == "uniswap_v3"
             else None)
    if quote is None:
        raise ValueError(f"unsupported pool_type {pool['pool_type']}")
    meta = {key: pool.get(key) for key in
            ("pool_id", "pool_type", "chain_id", "pool_address", "corridor",
             "stablecoin", "block_number", "snapshot_utc")}
    return [{**meta, **quote(pool, flow)} for flow in flows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", help="archived pool-state JSON")
    parser.add_argument("--out", default=None)
    parser.add_argument("--flows", nargs="*", type=float, default=DEFAULT_FLOWS)
    args = parser.parse_args()
    with open(args.snapshot, encoding="utf-8") as f:
        pool = json.load(f)
    rows = compute(pool, args.flows)
    for row in rows:
        row["snapshot_sha256"] = file_hash(args.snapshot)
    out = args.out or os.path.splitext(args.snapshot)[0] + "_execution_curve.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"wrote {len(rows)} exact DEX execution points -> {out}")


if __name__ == "__main__":
    main()
