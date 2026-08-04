#!/usr/bin/env python3
"""Unit tests for direction, adapters, and exact execution arithmetic."""
import json
import os
import sys
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from collect_exact_l2 import execute_sale, normalize_book, threshold_depth_rows, validate_book  # noqa: E402
from build_panel import layer_fiat, layer_tokenized, load_registry  # noqa: E402
from compute_dex_curves import constant_product_quote  # noqa: E402


def main():
    bids, asks = validate_book([(99.9, 10), (99.5, 20), (98.0, 50)],
                               [(100.1, 10), (100.5, 20)])
    sale = execute_sale(bids, 25, 100.0)
    assert sale["filled_stablecoin_usd"] == 25
    assert abs(sale["vwap_quote_per_stablecoin"] - 99.66) < 1e-9
    rows = threshold_depth_rows(bids, asks, {"venue": "test"})
    by_bps = {row["threshold_bps"]: row for row in rows}
    assert by_bps[10]["executable_stablecoin_usd"] == 10
    assert by_bps[50]["executable_stablecoin_usd"] == 30

    cp = constant_product_quote({"reserve_stablecoin": 1_000_000,
                                 "reserve_local_token": 5_000_000,
                                 "fee_bps": 30}, 10_000)
    assert 0 < cp["local_token_out"] < 50_000
    assert cp["average_slippage_bps"] > 30

    bitso = {"payload": {"bids": [{"price": "19.9", "amount": "5"}],
                            "asks": [{"price": "20.1", "amount": "6"}]}}
    nbids, nasks = normalize_book("bitso", bitso)
    assert nbids == [(19.9, 5.0)] and nasks == [(20.1, 6.0)]

    registry = load_registry()
    with tempfile.TemporaryDirectory() as temp:
        fiat_path = os.path.join(temp, "fiat.csv")
        pd.DataFrame([{"base": "USDT", "target": "TRY",
                       "market_identifier": "binance", "is_stale": False,
                       "is_anomaly": False, "cost_to_move_up_usd": 100,
                       "cost_to_move_down_usd": 40, "converted_volume_usd": 1000,
                       "bid_ask_spread_pct": 0.1, "snapshot_utc": "auto"}]).to_csv(
                           fiat_path, index=False)
        fiat = layer_fiat(fiat_path, "2026-08-03", registry)
        assert fiat.iloc[0]["depth_2pct_usd"] == 40

        token_path = os.path.join(temp, "token.csv")
        pd.DataFrame([{"peg_ccy": "BRL", "base": "BRZ", "target": "USDC",
                       "market_identifier": "uniswap_v3", "is_stale": False,
                       "is_anomaly": False, "cost_to_move_up_usd": 70,
                       "cost_to_move_down_usd": 30, "converted_volume_usd": 1000,
                       "bid_ask_spread_pct": 0.2, "snapshot_utc": "auto"}]).to_csv(
                           token_path, index=False)
        token = layer_tokenized(token_path, "2026-08-03", registry)
        assert token.iloc[0]["depth_2pct_usd"] == 70
    print("depth logic tests passed")


if __name__ == "__main__":
    main()
