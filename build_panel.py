#!/usr/bin/env python3
"""Build a corridor-date panel of CoinGecko's native 2% depth measure.

The program does not divide 2% cost-to-move by two and does not extrapolate to
larger shocks. It preserves USDT and USDC separately, adds an explicitly labeled
frictionless-sum upper bound, uses an explicit venue registry, chooses the
economically correct direction for each pair orientation, and constructs a full
date x corridor x asset skeleton. Missing API pairs remain missing, never zero.
"""
import glob
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_ROOT = os.path.join(HERE, "data", "snapshots")
OUT = os.path.join(HERE, "panel")
USD_ASSETS = {"USDT": "USDT", "USDC": "USDC", "TETHER": "USDT",
              "USD-COIN": "USDC"}
ASSETS = ["USDT", "USDC", "ALL_FRICTIONLESS"]


def as_bool(series):
    return series.astype(str).str.lower().eq("true")


def numeric(frame, columns):
    for column in columns:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_registry():
    registry = pd.read_csv(os.path.join(HERE, "venue_registry.csv"))
    return dict(zip(registry["market_identifier"].str.lower(),
                    registry["venue_type"].str.lower()))


def clean(frame):
    for column in ("is_stale", "is_anomaly"):
        if column not in frame:
            frame[column] = False
        frame[column] = as_bool(frame[column])
    frame = numeric(frame, ["cost_to_move_up_usd", "cost_to_move_down_usd",
                            "converted_volume_usd", "bid_ask_spread_pct"])
    frame["market_identifier"] = frame["market_identifier"].astype(str).str.lower()
    frame["base"] = frame["base"].astype(str).str.upper()
    frame["target"] = frame["target"].astype(str).str.upper()
    snapshot = (frame["snapshot_utc"] if "snapshot_utc" in frame
                else pd.Series("", index=frame.index))
    frame["manual_supplement"] = snapshot.astype(str).str.contains(
        "manual", case=False, na=False)
    return frame[~frame["is_stale"] & ~frame["is_anomaly"]].copy()


def summarize(rows, day, layer, source_scope="api_only"):
    if rows.empty:
        return pd.DataFrame()
    grouped = rows.groupby(["corridor", "asset"], dropna=False).agg(
        venues=("market_identifier", "nunique"), pairs=("market_identifier", "size"),
        volume_24h_usd=("converted_volume_usd", lambda x: x.sum(min_count=1)),
        depth_2pct_usd=("directional_depth_2pct_usd", lambda x: x.sum(min_count=1)),
        median_spread_pct=("bid_ask_spread_pct", "median"),
        unknown_venues=("venue_type", lambda x: int((x == "unknown").sum())),
    ).reset_index()
    grouped["date"] = day
    grouped["layer"] = layer
    grouped["source_scope"] = source_scope
    grouped["api_pair_observed"] = True
    grouped["observation_status"] = np.where(
        grouped["depth_2pct_usd"].notna(), "observed_depth",
        "observed_pair_missing_depth")
    grouped["aggregation_assumption"] = "asset_specific"
    return grouped


def add_frictionless_sum(frame):
    if frame.empty:
        return frame
    individual = frame[frame["asset"].isin(["USDT", "USDC"])].copy()
    combined = individual.groupby(["date", "layer", "corridor", "source_scope"],
                                  dropna=False).agg(
        venues=("venues", "sum"), pairs=("pairs", "sum"),
        volume_24h_usd=("volume_24h_usd", lambda x: x.sum(min_count=1)),
        depth_2pct_usd=("depth_2pct_usd", lambda x: x.sum(min_count=1)),
        median_spread_pct=("median_spread_pct", "median"),
        unknown_venues=("unknown_venues", "sum"),
        api_pair_observed=("api_pair_observed", "max")).reset_index()
    combined["asset"] = "ALL_FRICTIONLESS"
    combined["observation_status"] = np.where(
        combined["depth_2pct_usd"].notna(), "observed_depth",
        "not_observed_in_api")
    combined["aggregation_assumption"] = (
        "upper_bound_assuming_frictionless_USDT_USDC_conversion")
    return pd.concat([frame, combined], ignore_index=True, sort=False)


def layer_fiat(path, day, registry):
    frame = clean(pd.read_csv(path))
    frame = frame[~frame["manual_supplement"]].copy()
    frame["corridor"] = frame["target"]
    frame["asset"] = frame["base"].map(USD_ASSETS)
    # Selling base stablecoin for fiat walks down the bid side.
    frame["directional_depth_2pct_usd"] = frame["cost_to_move_down_usd"]
    frame["depth_direction"] = "sell_base_hit_bids_down"
    frame["venue_type"] = frame["market_identifier"].map(registry).fillna("unknown")
    frame = frame[frame["asset"].notna()]
    return summarize(frame, day, "fiat")


def layer_tokenized(path, day, registry):
    frame = clean(pd.read_csv(path))
    frame["corridor"] = frame["peg_ccy"].astype(str).str.upper()
    frame["venue_type"] = frame["market_identifier"].map(registry).fillna("unknown")
    # No target_coin_id heuristic: only explicitly registered DEX venues enter.
    frame = frame[frame["venue_type"] == "dex"].copy()

    quote_is_usd = frame["target"].isin(USD_ASSETS)
    base_is_usd = frame["base"].isin(USD_ASSETS)
    frame["asset"] = np.where(quote_is_usd, frame["target"].map(USD_ASSETS),
                              np.where(base_is_usd, frame["base"].map(USD_ASSETS), None))
    # local_token / USDC: off-ramp buys base -> price moves up.
    # USDC / local_token: off-ramp sells base -> base price moves down.
    frame["directional_depth_2pct_usd"] = np.where(
        quote_is_usd, frame["cost_to_move_up_usd"],
        np.where(base_is_usd, frame["cost_to_move_down_usd"], np.nan))
    frame = frame[frame["asset"].notna()]
    return summarize(frame, day, "tokenized")


def layer_available(manifest, filename):
    if filename in manifest.get("files", {}):
        return True
    # Backward compatibility for the July seed manifest.
    return False


def complete_skeleton(observed, days, manifests, corridors):
    skeleton = pd.MultiIndex.from_product(
        [days, ["fiat", "tokenized"], corridors, ASSETS],
        names=["date", "layer", "corridor", "asset"]).to_frame(index=False)
    panel = skeleton.merge(observed, on=["date", "layer", "corridor", "asset"],
                           how="left")
    for index, row in panel[panel["observation_status"].isna()].iterrows():
        manifest = manifests[row["date"]]
        filename = ("cg_fiat_pairs_depth.csv" if row["layer"] == "fiat"
                    else "cg_tickers_depth.csv")
        available = layer_available(manifest, filename) or os.path.exists(
            os.path.join(SNAPSHOT_ROOT, row["date"], filename))
        panel.loc[index, "observation_status"] = (
            "not_observed_in_api" if available else "layer_not_collected")
        panel.loc[index, "api_pair_observed"] = False
        panel.loc[index, "venues"] = 0 if available else np.nan
        panel.loc[index, "pairs"] = 0 if available else np.nan
        panel.loc[index, "source_scope"] = "api_only"
        panel.loc[index, "aggregation_assumption"] = (
            "upper_bound_assuming_frictionless_USDT_USDC_conversion"
            if row["asset"] == "ALL_FRICTIONLESS" else "asset_specific")
    return panel.sort_values(["date", "layer", "corridor", "asset"])


def write_manual_supplement(days):
    rows = []
    for day in days:
        path = os.path.join(SNAPSHOT_ROOT, day, "cg_fiat_pairs_depth.csv")
        if not os.path.exists(path):
            continue
        frame = clean(pd.read_csv(path))
        manual = frame[frame["manual_supplement"]].copy()
        if manual.empty:
            continue
        manual.insert(0, "date", day)
        rows.append(manual)
    output = os.path.join(OUT, "manual_supplement_rows.csv")
    (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "market_identifier"])).to_csv(output, index=False)


def main():
    os.makedirs(OUT, exist_ok=True)
    registry = load_registry()
    corridors = pd.read_csv(os.path.join(HERE, "corridors.csv"))["corridor"].tolist()
    frames, manifests, days = [], {}, []
    for directory in sorted(glob.glob(os.path.join(SNAPSHOT_ROOT, "*"))):
        day = os.path.basename(directory)
        manifest_path = os.path.join(directory, "manifest.json")
        if not os.path.exists(manifest_path):
            continue
        with open(manifest_path, encoding="utf-8") as f:
            manifests[day] = json.load(f)
        days.append(day)
        fiat = os.path.join(directory, "cg_fiat_pairs_depth.csv")
        tokenized = os.path.join(directory, "cg_tickers_depth.csv")
        if os.path.exists(fiat):
            frames.append(layer_fiat(fiat, day, registry))
        if os.path.exists(tokenized):
            frames.append(layer_tokenized(tokenized, day, registry))
    if not days:
        print("no valid snapshots found")
        return
    nonempty = [frame for frame in frames if not frame.empty]
    observed = pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame(
        columns=["date", "layer", "corridor", "asset"])
    observed = add_frictionless_sum(observed)
    panel = complete_skeleton(observed, days, manifests, corridors)
    panel.to_csv(os.path.join(OUT, "panel_depth_2pct.csv"), index=False)
    write_manual_supplement(days)

    valid = panel[panel["observation_status"] == "observed_depth"]
    summary = (valid.groupby(["layer", "corridor", "asset"])["depth_2pct_usd"]
               .agg(n_days="count", median="median",
                    p10=lambda x: x.quantile(0.10),
                    p90=lambda x: x.quantile(0.90), min="min", max="max")
               .reset_index())
    summary.to_csv(os.path.join(OUT, "summary_depth_2pct.csv"), index=False)
    print(f"panel: {len(days)} dates x {len(corridors)} corridors x 2 layers x "
          f"{len(ASSETS)} asset definitions = {len(panel)} rows")
    print(panel["observation_status"].value_counts().to_string())


if __name__ == "__main__":
    main()
