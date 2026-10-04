"""Offline contract/error tests; fixtures are synthetic, not research observations."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import collect_exact_l2 as l2


class NativeCollectionTests(unittest.TestCase):
    def test_adapters(self):
        sides = {"bids": [["9", "2"]], "asks": [["11", "3"]]}
        fixtures = {
            "btcturk": {"success": True, "data": sides},
            "mercadobitcoin": sides,
            "indodax": {"buy": sides["bids"], "sell": sides["asks"]},
            "bitkub": {"error": 0, "result": sides},
        }
        for venue, payload in fixtures.items():
            with self.subTest(venue=venue):
                self.assertEqual(l2.normalize_book(venue, payload), ([(9., 2.)], [(11., 3.)]))

    def test_exchange_api_errors(self):
        for venue, payload in [("btcturk", {"success": False}), ("bitkub", {"error": 1})]:
            with self.assertRaises(ValueError):
                l2.normalize_book(venue, payload)

    def test_invalid_values_fail_closed(self):
        for bad in [(float("nan"), 1), (1, float("inf")), (0, 1), (1, -1)]:
            with self.assertRaises(ValueError):
                l2.validate_book([bad], [(2, 1)])
        with self.assertRaises(ValueError):
            l2.validate_book([(2, 1)], [(1, 1)])

    def test_sorting_and_zero_quantity(self):
        self.assertEqual(l2.validate_book([(8, 1), (9, 2), (7, 0)], [(11, 3), (10, 1)]),
                         ([(9, 2), (8, 1)], [(10, 1), (11, 3)]))

    def test_timestamp_units(self):
        expected = "2026-10-04T00:00:00+00:00"
        value = datetime.fromisoformat(expected).timestamp()
        self.assertEqual(l2.exchange_timestamp("btcturk", {"data": {"timestamp": value * 1000}}), expected)
        for v in [value, value * 1000]:
            self.assertEqual(l2.exchange_timestamp("mercadobitcoin", {"timestamp": v}), expected)
        self.assertIsNone(l2.exchange_timestamp("indodax", {"server_time": value}))

    def test_coverage_requires_all_corridors(self):
        attempts = [{"venue": "test", "pair": c, "corridor": c, "status": "success"}
                    for c in l2.REQUIRED_CORRIDORS]
        self.assertEqual(l2.summarize_coverage(attempts)["status"], "complete")
        self.assertEqual(l2.summarize_coverage(attempts[:-1])["status"], "partial")
        self.assertEqual(l2.summarize_coverage([])["status"], "partial")
        attempts.append({"venue": "other", "pair": "x", "corridor": "TRY", "status": "failed"})
        self.assertEqual(l2.summarize_coverage(attempts)["status"], "partial")

    def config(self, root):
        path = root / "config.csv"
        path.write_text("venue,pair,corridor,stablecoin,stablecoin_side,enabled,limit,note\n"
                        "btcturk,USDTTRY,TRY,USDT,base,true,100,test\n"
                        "binance,USDTTRY,TRY,USDT,base,false,5000,restricted\n")
        return path

    @patch.object(l2, "request_json")
    def test_same_day_is_append_only_and_disabled_never_requested(self, request):
        request.return_value = ({"success": True, "data": {"bids": [[9, 2]], "asks": [[11, 3]]}},
                                SimpleNamespace(status_code=200))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config(root)
            legacy = root / "out" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
            legacy.mkdir(parents=True)
            (legacy / "manifest.json").write_text('{"legacy":true}')
            first = l2.collect(config, root / "out")
            second = l2.collect(config, root / "out")
            self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(request.call_count, 2)
            self.assertEqual(first["attempts"][1]["status"], "disabled")
            self.assertEqual(first["coverage"]["status"], "partial")
            self.assertEqual((legacy / "manifest.json").read_text(), '{"legacy":true}')
            self.assertEqual(len(list(legacy.glob("runs/*/manifest.json"))), 2)
            self.assertEqual(first["attempts"][0]["corridor"], "TRY")

    @patch.object(l2, "request_json", side_effect=l2.CollectionError("HTTP 451", 451, "access_restricted"))
    def test_failures_are_archived(self, request):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = l2.collect(self.config(root), root / "out", summary_path=root / "summary.md")
            self.assertEqual(result["attempts"][0]["http_status"], 451)
            self.assertEqual(result["attempts"][0]["error_kind"], "access_restricted")
            self.assertEqual(result["coverage"]["successful_sources"], 0)
            self.assertIn("partial", (root / "summary.md").read_text())
            self.assertEqual(len(list((root / "out").glob("*/runs/*/manifest.json"))), 1)

    def test_no_backdating(self):
        with self.assertRaisesRegex(ValueError, "cannot be backdated"):
            l2.collect("unused", "unused", day="1999-01-01")

    @patch("requests.get")
    @patch.object(l2.time, "sleep")
    def test_451_is_not_retried_or_rerouted(self, sleep, get):
        get.return_value = SimpleNamespace(status_code=451, text="restricted")
        with self.assertRaises(l2.CollectionError) as caught:
            l2.request_json("https://example.invalid/book", {})
        self.assertEqual(caught.exception.error_kind, "access_restricted")
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    @patch("requests.get")
    def test_html_200_is_not_a_book(self, get):
        response = SimpleNamespace(status_code=200)
        response.json = lambda: (_ for _ in ()).throw(ValueError("html"))
        get.return_value = response
        with self.assertRaisesRegex(l2.CollectionError, "not valid JSON"):
            l2.request_json("https://example.invalid/book", {})

    def test_units_and_truncated_depth(self):
        rows = l2.threshold_depth_rows([(99.9, 2)], [(100.1, 1)], {})
        self.assertTrue(rows[-1]["depth_is_lower_bound"])
        self.assertEqual(rows[-1]["executable_stablecoin_units"], 2)
        sale = l2.execute_sale([(99.9, 2)], 5, 100)
        self.assertEqual(sale["filled_stablecoin_units"], 2)
        self.assertTrue(sale["book_exhausted"])


if __name__ == "__main__":
    unittest.main()
