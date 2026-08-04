#!/usr/bin/env python3
"""Daily two-layer off-ramp depth snapshot.

Runs the paper's own pullers (vendored, unchanged, so the measurement is
methodologically identical to the July 2026 snapshots in the manuscript):
  Layer 2: puller_layer2.py  (USDT/USDC order books quoted in EM fiat)
  Layer 1: puller_layer1.py --skip-chart (tokenized local-currency coins)

Flat layout: every file sits in one directory (no subfolders to upload), so the
whole project can be created with a plain multi-file upload. Output folders
(data/, panel/) are created automatically at runtime.

On the first run, seed_2026-07-08_cg_fiat_pairs_depth.csv is bootstrapped into
data/snapshots/2026-07-08/ so the July observation from the paper stays as the
panel's first date.

Archives outputs to data/snapshots/YYYY-MM-DD/ with a sha256 manifest, then
rebuilds the panel. Idempotent per day (rerun exits unless --force).

Env: CG_DEMO_KEY (optional, free CoinGecko demo key; cuts runtime ~5x).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
VEND = HERE


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_puller(script, workdir, key, extra=()):
    cmd = [sys.executable, os.path.join(VEND, script), *extra]
    if key:
        cmd += ["--key", key]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=workdir)
    return {"script": script, "returncode": r.returncode,
            "seconds": round(time.time() - t0, 1)}


def bootstrap_seed():
    """Place the July 2026 seed snapshot (shipped flat) into data/snapshots/."""
    seed_src = os.path.join(HERE, "seed_2026-07-08_cg_fiat_pairs_depth.csv")
    seed_dir = os.path.join(HERE, "data", "snapshots", "2026-07-08")
    seed_dst = os.path.join(seed_dir, "cg_fiat_pairs_depth.csv")
    if not os.path.exists(seed_src) or os.path.exists(seed_dst):
        return
    os.makedirs(seed_dir, exist_ok=True)
    shutil.copy2(seed_src, seed_dst)
    with open(os.path.join(seed_dir, "manifest.json"), "w") as f:
        json.dump({"date": "2026-07-08",
                   "note": "seed snapshot from the paper replication package "
                           "(manual Bitso rows included); bootstrapped from the "
                           "flat layout on first run",
                   "files": {"cg_fiat_pairs_depth.csv":
                             {"sha256": sha256(seed_dst)}}}, f, indent=2)
    print("bootstrapped July 2026 seed snapshot into data/snapshots/2026-07-08/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-layer1", action="store_true")
    ap.add_argument("--skip-layer2", action="store_true")
    ap.add_argument("--date", default=None, help="override snapshot date (UTC)")
    args = ap.parse_args()

    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snap_dir = os.path.join(HERE, "data", "snapshots", day)
    manifest_path = os.path.join(snap_dir, "manifest.json")
    if os.path.exists(manifest_path) and not args.force:
        print(f"snapshot {day} already collected; use --force to redo")
        return

    bootstrap_seed()
    key = os.environ.get("CG_DEMO_KEY") or None
    work = os.path.join(HERE, "data", "_work", day)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    runs = []
    if not args.skip_layer2:
        runs.append(run_puller("coingecko_fiat_pairs.py", work, key))
    if not args.skip_layer1:
        runs.append(run_puller("coingecko_puller.py", work, key,
                               extra=("--skip-chart",)))
    fails = [r for r in runs if r["returncode"] != 0]

    cg = os.path.join(work, "cg_data")
    os.makedirs(snap_dir, exist_ok=True)
    files = {}
    if os.path.isdir(cg):
        for fn in sorted(os.listdir(cg)):
            if not fn.endswith((".csv", ".jsonl")):
                continue
            src = os.path.join(cg, fn)
            dst = os.path.join(snap_dir, fn)
            shutil.copy2(src, dst)
            line_count = sum(1 for _ in open(dst, encoding="utf-8"))
            files[fn] = {"sha256": sha256(dst), "bytes": os.path.getsize(dst),
                         "rows": max(0, line_count - (1 if fn.endswith('.csv') else 0))}
    manifest = {"date": day,
                "collected_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "keyed": bool(key), "runs": runs, "files": files,
                "code_sha256": {
                    "collect_snapshot.py": sha256(__file__),
                    "build_panel.py": sha256(os.path.join(HERE, "build_panel.py")),
                    **{s: sha256(os.path.join(HERE, s))
                       for s in ("puller_layer1.py", "puller_layer2.py")},
                    **{s: sha256(os.path.join(HERE, s)) for s in
                       ("corridors.csv", "venue_registry.csv")}}}
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    shutil.rmtree(work, ignore_errors=True)
    print(f"snapshot {day}: {len(files)} files -> {snap_dir}")

    subprocess.run([sys.executable, os.path.join(HERE, "build_panel.py")], cwd=HERE)
    if fails or not files:
        sys.exit(f"collection incomplete: fails={fails} files={list(files)}")


if __name__ == "__main__":
    main()
