#!/usr/bin/env python3
"""Offline tests of native L2 consolidation and immutable raw provenance."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from build_exact_l2_panel import build_panel, main, sha256


class NativeL2PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_csv(self, path, rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def snapshot(self, day="2026-09-07", run=None, venue="valr", failed=False):
        directory = self.root / "data" / "exact_l2" / day
        if run:
            directory = directory / "runs" / run
        directory.mkdir(parents=True)
        raw = directory / f"{venue}_usdtzar_raw.json"
        raw.write_text('{"Bids":[[16.1,20]],"Asks":[[16.2,20]]}', encoding="utf-8")
        meta = {
            "date": day, "collected_utc": day + "T10:00:00+00:00",
            "venue": venue, "pair": "USDTZAR", "corridor": "ZAR",
            "stablecoin": "USDT", "off_ramp_action": "sell_stablecoin_base_hit_bids",
            "raw_file": raw.name, "raw_sha256": sha256(raw),
        }
        if run:
            meta.update({
                "snapshot_id": run, "base_asset": "USDT", "quote_asset": "ZAR",
                "source_type": "native_l2", "received_utc": day + "T10:00:01+00:00",
                "exchange_timestamp_utc": "", "exchange_timestamp_status": "unavailable",
                "price_unit": "ZAR per USDT", "quantity_unit": "USDT",
                "usd_conversion_assumption": "1 stablecoin token = 1 USD; no FX conversion",
                "source_url": "https://example.test/book", "source_params_json": "{}",
            })
        depth = {**meta, "threshold_bps": "10", "reference_mid": "16.15",
                 "best_bid": "16.1", "best_ask": "16.2",
                 "executable_stablecoin_usd": "20", "quote_proceeds": "322",
                 "book_covers_threshold": "True", "depth_is_lower_bound": "False",
                 "levels_used": "1"}
        curve = {**meta, "requested_stablecoin_usd": "10",
                 "filled_stablecoin_usd": "10", "fill_rate": "1",
                 "quote_proceeds": "161", "vwap_quote_per_stablecoin": "16.1",
                 "average_slippage_bps": "30.959", "terminal_impact_bps": "30.959",
                 "levels_used": "1", "book_exhausted": "False"}
        for filename, row in (("threshold_depth.csv", depth), ("execution_curves.csv", curve)):
            if failed:
                (directory / filename).write_text("\n", encoding="utf-8")
            else:
                self.write_csv(directory / filename, [row])
        attempt = {"venue": venue, "pair": "USDTZAR", "status": "success",
                   "raw_file": raw.name, "raw_sha256": sha256(raw)}
        if failed:
            attempt = {"venue": venue, "pair": "USDTZAR", "status": "failed",
                       "error": "HTTP 451"}
        manifest = {"date": day, "created_utc": day + "T10:00:02+00:00",
                    "attempts": [attempt], "outputs": {}}
        if run:
            manifest.update(schema_version=2, snapshot_id=run)
        self.write_manifest(directory, manifest)
        return directory

    def write_manifest(self, directory, manifest):
        manifest["outputs"] = {name: sha256(directory / name) for name in (
            "threshold_depth.csv", "execution_curves.csv")}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def manifest(self, directory):
        return json.loads((directory / "manifest.json").read_text())

    def panel(self, kind="threshold_depth"):
        with (self.root / "panel" / f"native_l2_{kind}.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_legacy_and_every_run_are_retained_without_aggregation(self):
        self.snapshot()
        self.snapshot(run="first", venue="another")
        self.snapshot(run="second")
        result = build_panel(self.root)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["snapshots_discovered"], 3)
        rows = self.panel()
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["source_snapshot_id"] for r in rows},
                         {"legacy:2026-09-07", "first", "second"})
        self.assertEqual({r["venue"] for r in rows}, {"valr", "another"})
        self.assertTrue(all(r["source_type"] == "native_l2" for r in rows))
        self.assertEqual(len(self.panel("execution_curves")), 3)

    def test_raw_and_csv_provenance_resolves_from_repository_root(self):
        self.snapshot(run="first")
        build_panel(self.root)
        row = self.panel()[0]
        for stem in ("raw", "csv", "manifest"):
            path = self.root / row[f"source_{stem}_path"]
            self.assertTrue(path.is_file())
            self.assertEqual(sha256(path), row[f"source_{stem}_sha256"])
        self.assertEqual(row["raw_file"], "valr_usdtzar_raw.json")
        self.assertEqual(row["base_asset"], "USDT")
        self.assertEqual(row["quote_asset"], "ZAR")
        self.assertEqual(row["collected_utc"], "2026-09-07T10:00:00+00:00")
        self.assertEqual(row["received_utc"], "2026-09-07T10:00:01+00:00")

    def test_legacy_metadata_is_labeled_without_fabricated_values(self):
        self.snapshot()
        build_panel(self.root)
        row = self.panel()[0]
        self.assertEqual(row["source_metadata_status"], "legacy")
        self.assertEqual(row["source_snapshot_id_status"], "legacy_directory_identifier")
        for field in ("base_asset", "quote_asset", "received_utc", "snapshot_id",
                      "exchange_timestamp_utc", "source_url"):
            self.assertEqual(row[field], "")
        self.assertEqual(row["exchange_timestamp_status"], "legacy_unavailable")
        self.assertIn("base_asset", json.loads(row["metadata_missing_fields"]))

    def test_legacy_usd_columns_are_kept_as_token_aliases(self):
        self.snapshot()
        build_panel(self.root)
        row = self.panel()[0]
        self.assertEqual(row["executable_stablecoin_units"], "20")
        self.assertEqual(row["executable_stablecoin_usd"], "20")
        self.assertEqual(row["quantity_unit"], "stablecoin_tokens")
        self.assertIn("1 stablecoin token = 1 USD", row["usd_conversion_assumption"])
        curve = self.panel("execution_curves")[0]
        for kind in ("requested", "filled"):
            self.assertEqual(curve[f"{kind}_stablecoin_units"], "10")
            self.assertEqual(curve[f"{kind}_stablecoin_usd"], "10")

    def test_blank_failed_snapshots_produce_no_phantom_rows(self):
        self.snapshot(failed=True)
        result = build_panel(self.root)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["failed_books_declared"], 1)
        self.assertEqual(self.panel(), [])
        self.assertEqual(self.panel("execution_curves"), [])

    def test_no_input_still_writes_headers_and_audit(self):
        result = build_panel(self.root)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["snapshots_discovered"], 0)
        self.assertEqual(self.panel(), [])
        saved = json.loads((self.root / "panel" / "native_l2_panel_audit.json").read_text())
        self.assertEqual(saved["integrity_status"], "passed")

    def test_coin_gecko_and_previous_panels_are_never_discovered(self):
        self.snapshot()
        proxy = self.root / "data" / "snapshots" / "2026-09-07"
        proxy.mkdir(parents=True)
        (proxy / "manifest.json").write_text("invalid")
        (proxy / "threshold_depth.csv").write_text("proxy")
        build_panel(self.root)
        before = (self.root / "panel" / "native_l2_threshold_depth.csv").read_bytes()
        result = build_panel(self.root)
        self.assertEqual(result["snapshots_discovered"], 1)
        self.assertEqual(before, (self.root / "panel" / "native_l2_threshold_depth.csv").read_bytes())
        self.assertEqual(len(self.panel()), 1)

    def test_raw_tampering_fails_but_other_snapshot_remains(self):
        bad = self.snapshot()
        self.snapshot(run="verified")
        (bad / "valr_usdtzar_raw.json").write_text("tampered")
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertTrue(any("SHA256 mismatch" in e["error"] for e in result["errors"]))
        self.assertEqual([r["source_snapshot_id"] for r in self.panel()], ["verified"])

    def test_csv_tampering_rejects_only_affected_output(self):
        directory = self.snapshot()
        with (directory / "threshold_depth.csv").open("a") as handle:
            handle.write("\n")
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])
        self.assertEqual(len(self.panel("execution_curves")), 1)

    def test_missing_raw_file_is_integrity_failure(self):
        directory = self.snapshot()
        (directory / "valr_usdtzar_raw.json").unlink()
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_rows_for_failed_books_are_rejected(self):
        directory = self.snapshot()
        manifest = self.manifest(directory)
        manifest["attempts"][0]["status"] = "failed"
        self.write_manifest(directory, manifest)
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_raw_path_traversal_is_rejected(self):
        directory = self.snapshot()
        manifest = self.manifest(directory)
        manifest["attempts"][0]["raw_file"] = "../valr_usdtzar_raw.json"
        self.write_manifest(directory, manifest)
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_non_native_source_type_is_rejected_even_with_valid_hashes(self):
        directory = self.snapshot(run="first")
        with (directory / "threshold_depth.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["source_type"] = "coingecko_proxy"
        self.write_csv(directory / "threshold_depth.csv", rows)
        self.write_manifest(directory, self.manifest(directory))
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_disagreeing_token_alias_is_rejected(self):
        directory = self.snapshot()
        with (directory / "threshold_depth.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["executable_stablecoin_units"] = "999"
        self.write_csv(directory / "threshold_depth.csv", rows)
        self.write_manifest(directory, self.manifest(directory))
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_non_native_manifest_is_rejected(self):
        directory = self.snapshot()
        manifest = self.manifest(directory)
        manifest["source_type"] = "coingecko_proxy"
        self.write_manifest(directory, manifest)
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_successful_csv_requires_native_depth_metrics(self):
        directory = self.snapshot()
        with (directory / "threshold_depth.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        del rows[0]["threshold_bps"]
        self.write_csv(directory / "threshold_depth.csv", rows)
        self.write_manifest(directory, self.manifest(directory))
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_malformed_manifest_does_not_hide_other_snapshots(self):
        directory = self.snapshot()
        (directory / "manifest.json").write_text("not JSON")
        self.snapshot(run="good")
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(len(self.panel()), 1)

    def test_manifest_date_must_match_source_day(self):
        directory = self.snapshot()
        manifest = self.manifest(directory)
        manifest["date"] = "2026-09-06"
        self.write_manifest(directory, manifest)
        result = build_panel(self.root)
        self.assertEqual(result["integrity_status"], "failed")
        self.assertEqual(self.panel(), [])

    def test_successful_book_with_blank_output_is_integrity_error(self):
        directory = self.snapshot()
        (directory / "threshold_depth.csv").write_text("\n")
        self.write_manifest(directory, self.manifest(directory))
        result = build_panel(self.root)
        self.assertTrue(any("has no valid rows" in e["error"] for e in result["errors"]))

    def test_cli_signals_invalid_provenance(self):
        directory = self.snapshot()
        (directory / "valr_usdtzar_raw.json").unlink()
        self.assertEqual(main(["--repo-root", str(self.root)]), 1)

    def test_outputs_cannot_be_written_into_snapshot_inputs(self):
        with self.assertRaisesRegex(ValueError, "output directory"):
            build_panel(self.root, self.root / "data" / "exact_l2" / "panel")


if __name__ == "__main__":
    unittest.main()
