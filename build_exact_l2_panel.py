#!/usr/bin/env python3
"""Build provenance-checked native L2 panels, without any proxy observations.

Reads only data/exact_l2/YYYY-MM-DD/{manifest.json,runs/*/manifest.json}.
Each successful book remains a separate observation in its original snapshot;
there is no daily/venue aggregation, gap filling, or historical reconstruction.
Legacy ``*_stablecoin_usd`` columns are retained verbatim for compatibility but
represent token quantities, not a measured USD valuation. Explicit ``*_units``
aliases and the unit-peg assumption make that distinction visible.

The CLI writes valid rows and an audit even if some inputs are invalid, then
exits nonzero for integrity errors. Failed/disabled collection attempts are
counted in the audit; they do not create missing-book placeholder observations.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


HERE = Path(__file__).resolve().parent
USD_ASSUMPTION = "1 stablecoin token = 1 USD; no FX conversion"
PROVENANCE_COLUMNS = [
    "source_type", "source_snapshot_id", "source_snapshot_id_status",
    "source_snapshot_dir", "source_snapshot_layout", "source_metadata_status",
    "source_manifest_path", "source_manifest_sha256", "source_csv_path",
    "source_csv_sha256", "source_raw_path", "source_raw_sha256",
    "provenance_status", "metadata_missing_fields",
]
COMMON_COLUMNS = [
    "date", "snapshot_id", "collected_utc", "received_utc",
    "exchange_timestamp_utc", "exchange_timestamp_status", "venue", "pair",
    "corridor", "stablecoin", "base_asset", "quote_asset", "price_unit",
    "quantity_unit", "usd_conversion_assumption", "source_url",
    "source_params_json", "off_ramp_action", "raw_file", "raw_sha256",
]
THRESHOLD_COLUMNS = [
    "threshold_bps", "reference_mid", "best_bid", "best_ask",
    "executable_stablecoin_units", "executable_stablecoin_usd", "quote_proceeds",
    "book_covers_threshold", "depth_is_lower_bound", "levels_used",
]
EXECUTION_COLUMNS = [
    "requested_stablecoin_units", "requested_stablecoin_usd",
    "filled_stablecoin_units", "filled_stablecoin_usd", "fill_rate",
    "quote_proceeds", "vwap_quote_per_stablecoin", "average_slippage_bps",
    "terminal_impact_bps", "levels_used", "book_exhausted",
]
OUTPUTS = {
    "threshold_depth.csv": ("native_l2_threshold_depth.csv", THRESHOLD_COLUMNS),
    "execution_curves.csv": ("native_l2_execution_curves.csv", EXECUTION_COLUMNS),
}
UNIT_ALIASES = {
    "executable_stablecoin_units": "executable_stablecoin_usd",
    "requested_stablecoin_units": "requested_stablecoin_usd",
    "filled_stablecoin_units": "filled_stablecoin_usd",
}
HASH_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_manifests(input_root):
    """Discover only supported snapshot layouts, never recursively glob CSVs."""
    if not input_root.exists():
        return
    for day_dir in sorted(input_root.iterdir()):
        try:
            if date.fromisoformat(day_dir.name).isoformat() != day_dir.name:
                continue
        except ValueError:
            continue
        if not day_dir.is_dir():
            continue
        if (day_dir / "manifest.json").is_file():
            yield day_dir / "manifest.json", "legacy_day_root", day_dir.name
        runs = day_dir / "runs"
        if runs.is_dir():
            for run_dir in sorted(runs.iterdir()):
                if run_dir.is_dir() and (run_dir / "manifest.json").is_file():
                    yield run_dir / "manifest.json", "append_only_run", day_dir.name


def relative(path, root):
    return Path(path).resolve().relative_to(root).as_posix()


def raw_path(value, snapshot_dir, root):
    """Accept snapshot-relative or repo-relative raw paths within this snapshot."""
    path = Path(str(value))
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("raw_file must be a safe relative path")
    candidates = (snapshot_dir / path, root / path)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(snapshot_dir.resolve()) and resolved.is_file():
            return resolved
    raise ValueError(f"raw file missing or outside its snapshot: {value}")


def checked_hash(path, expected):
    if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
        raise ValueError(f"missing or invalid SHA256 for {path.name}")
    actual = sha256(path)
    if actual != expected.lower():
        raise ValueError(f"SHA256 mismatch for {path.name}")
    return actual


def read_rows(path):
    content = path.read_text(encoding="utf-8-sig")
    if not content.strip():
        return []
    reader = csv.DictReader(io.StringIO(content, newline=""))
    fields = reader.fieldnames or []
    if not fields or len(set(fields)) != len(fields) or any(not f for f in fields):
        raise ValueError("invalid or duplicate CSV column names")
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("CSV row has a different column count from its header")
        if any(str(value).strip() for value in row.values()):
            rows.append(row)
    return rows


def enrich_row(row, *, manifest, snapshot_dir, layout, day, root, provenance):
    if row.get("source_type") not in (None, "", "native_l2"):
        raise ValueError("non-native source_type in native L2 input")
    for key in ("date", "collected_utc", "venue", "pair", "corridor", "stablecoin"):
        if not row.get(key):
            raise ValueError(f"row is missing required field {key}")
    if row["date"] != day:
        raise ValueError("row date does not match snapshot directory")
    snapshot_id = manifest.get("snapshot_id")
    if snapshot_id and row.get("snapshot_id") not in (None, "", snapshot_id):
        raise ValueError("row snapshot_id does not match manifest")
    result = dict(row)
    for units, legacy in UNIT_ALIASES.items():
        if legacy not in row and units not in row:
            continue
        if row.get(units) and row.get(legacy):
            try:
                equal = Decimal(row[units]) == Decimal(row[legacy])
            except InvalidOperation:
                equal = False
            if not equal:
                raise ValueError(f"{units} disagrees with legacy {legacy}")
        elif row.get(legacy):
            result[units] = row[legacy]
    legacy_schema = manifest.get("schema_version", 1) == 1
    result.setdefault("usd_conversion_assumption", USD_ASSUMPTION)
    if not result.get("usd_conversion_assumption"):
        result["usd_conversion_assumption"] = USD_ASSUMPTION
    # Old rows explicitly describe stablecoin-base sales, but do not record
    # exchange base/quote asset metadata. Do not guess that metadata from symbols.
    if not result.get("quantity_unit"):
        result["quantity_unit"] = "stablecoin_tokens"
    if not result.get("exchange_timestamp_status"):
        result["exchange_timestamp_status"] = (
            "legacy_unavailable" if legacy_schema else "unavailable")
    missing = [key for key in (
        "base_asset", "quote_asset", "received_utc", "exchange_timestamp_utc",
        "source_url", "source_params_json") if not row.get(key)]
    result.update(provenance)
    result.update({
        "source_type": "native_l2",
        "source_snapshot_id": snapshot_id or (
            f"legacy:{day}" if layout == "legacy_day_root" else snapshot_dir.name),
        "source_snapshot_id_status": "recorded" if snapshot_id else (
            "legacy_directory_identifier" if layout == "legacy_day_root"
            else "directory_identifier"),
        "source_snapshot_dir": relative(snapshot_dir, root),
        "source_snapshot_layout": layout,
        "source_metadata_status": "legacy" if legacy_schema else "recorded",
        "provenance_status": "sha256_verified",
        "metadata_missing_fields": json.dumps(missing, separators=(",", ":")),
    })
    return result


def atomic_write(path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            write(handle)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def build_panel(repo_root=HERE, output_dir=None):
    """Write independent panels and audit; return audit including integrity errors."""
    root = Path(repo_root).resolve()
    input_root = root / "data" / "exact_l2"
    out = Path(output_dir).resolve() if output_dir else root / "panel"
    if out.is_relative_to(input_root.resolve()):
        raise ValueError("output directory must not be inside native snapshot inputs")
    panels = {name: [] for name in OUTPUTS}
    audit = {
        "source_type": "native_l2", "snapshots_discovered": 0,
        "successful_books_declared": 0, "failed_books_declared": 0,
        "disabled_books_declared": 0, "rows_written": {}, "errors": [],
        "usd_conversion_assumption": USD_ASSUMPTION,
        "legacy_metadata_policy": "Unavailable legacy fields are blank and labeled legacy; no backfill.",
    }

    def error(path, message):
        audit["errors"].append({"source": str(path), "error": str(message)})

    for manifest_path, layout, day in snapshot_manifests(input_root):
        audit["snapshots_discovered"] += 1
        snapshot_dir = manifest_path.parent
        try:
            manifest_rel = relative(manifest_path, root)
            if not manifest_path.resolve().is_relative_to(input_root.resolve()):
                raise ValueError("snapshot manifest is outside native L2 inputs")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("date") != day:
                raise ValueError("manifest date does not match snapshot directory")
            if manifest.get("source_type") not in (None, "", "native_l2"):
                raise ValueError("non-native source_type in snapshot manifest")
            if not isinstance(manifest.get("attempts"), list):
                raise ValueError("manifest attempts must be a list")
            if not isinstance(manifest.get("outputs"), dict):
                raise ValueError("manifest outputs must contain CSV SHA256 values")
            manifest_hash = sha256(manifest_path)
        except (ValueError, OSError) as exc:
            error(manifest_path, exc)
            continue
        successes = {}
        ambiguous = set()
        for attempt in manifest["attempts"]:
            if not isinstance(attempt, dict):
                error(manifest_rel, "invalid attempt entry")
                continue
            status = attempt.get("status")
            if status in ("success", "failed", "disabled"):
                counter = {"success": "successful", "failed": "failed", "disabled": "disabled"}[status]
                audit[f"{counter}_books_declared"] += 1
            if status != "success":
                continue
            key = (attempt.get("venue"), attempt.get("pair"))
            if key in successes or key in ambiguous:
                error(manifest_rel, f"duplicate successful attempt for {key}")
                successes.pop(key, None)
                ambiguous.add(key)
                continue
            try:
                raw = raw_path(attempt.get("raw_file"), snapshot_dir, root)
                raw_hash = checked_hash(raw, attempt.get("raw_sha256"))
                successes[key] = (attempt, raw, raw_hash)
            except (ValueError, OSError) as exc:
                error(manifest_rel, f"successful book {key}: {exc}")

        for filename in OUTPUTS:
            csv_path = snapshot_dir / filename
            try:
                if not csv_path.resolve().is_relative_to(snapshot_dir.resolve()):
                    raise ValueError("CSV is outside its snapshot")
                csv_hash = checked_hash(csv_path, manifest["outputs"].get(filename))
                rows = read_rows(csv_path)
            except (ValueError, OSError, csv.Error) as exc:
                error(relative(snapshot_dir, root) + "/" + filename, exc)
                continue
            present = set()
            for number, row in enumerate(rows, start=2):
                try:
                    key = (row.get("venue"), row.get("pair"))
                    if key not in successes:
                        raise ValueError(f"row has no verified successful book for {key}")
                    for metric in OUTPUTS[filename][1]:
                        if metric in UNIT_ALIASES or metric in UNIT_ALIASES.values():
                            continue
                        if metric not in row:
                            raise ValueError(f"row is missing required native L2 metric {metric}")
                    quantities = ("executable",) if filename == "threshold_depth.csv" else (
                        "requested", "filled")
                    for quantity in quantities:
                        if not any(row.get(f"{quantity}_stablecoin_{unit}") for unit in ("usd", "units")):
                            raise ValueError(f"row is missing {quantity} stablecoin quantity")
                    attempt, raw, raw_hash = successes[key]
                    if raw_path(row.get("raw_file"), snapshot_dir, root) != raw:
                        raise ValueError("row raw_file does not match successful attempt")
                    if row.get("raw_sha256", "").lower() != raw_hash:
                        raise ValueError("row raw_sha256 does not match successful attempt")
                    for field in ("corridor", "base_asset", "quote_asset"):
                        if attempt.get(field) and row.get(field) != attempt[field]:
                            raise ValueError(f"row {field} does not match successful attempt")
                    enriched = enrich_row(
                        row, manifest=manifest, snapshot_dir=snapshot_dir,
                        layout=layout, day=day, root=root, provenance={
                            "source_manifest_path": manifest_rel,
                            "source_manifest_sha256": manifest_hash,
                            "source_csv_path": relative(csv_path, root),
                            "source_csv_sha256": csv_hash,
                            "source_raw_path": relative(raw, root),
                            "source_raw_sha256": raw_hash,
                        })
                    panels[filename].append(enriched)
                    present.add(key)
                except (ValueError, OSError) as exc:
                    error(f"{relative(csv_path, root)}:{number}", exc)
            for key in successes.keys() - present:
                error(relative(csv_path, root), f"successful book {key} has no valid rows")

    for filename, (output_name, specific_columns) in OUTPUTS.items():
        rows = panels[filename]
        columns = PROVENANCE_COLUMNS + COMMON_COLUMNS + specific_columns
        columns += sorted({key for row in rows for key in row} - set(columns))

        def write_csv(handle, rows=rows, columns=columns):
            writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

        atomic_write(out / output_name, write_csv)
        audit["rows_written"][output_name] = len(rows)
    audit["integrity_status"] = "failed" if audit["errors"] else "passed"
    atomic_write(out / "native_l2_panel_audit.json",
                 lambda handle: json.dump(audit, handle, indent=2, sort_keys=True))
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=HERE)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    audit = build_panel(args.repo_root, args.output_dir)
    for name, count in audit["rows_written"].items():
        print(f"{name}: {count} rows")
    print(f"native L2 provenance: {audit['integrity_status']}; "
          f"{len(audit['errors'])} integrity errors")
    return 1 if audit["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
