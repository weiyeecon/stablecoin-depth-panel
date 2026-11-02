"""Offline safety contracts for orchestration and proxy idempotence."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect_snapshot


class WorkflowSafetyTests(unittest.TestCase):
    def test_existing_proxy_snapshot_does_not_call_network_or_change_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = '2026-11-02'
            root = Path(tmp) / 'data' / 'snapshots' / day
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text(json.dumps({'date': day}))
            (root / 'cg_fiat_pairs_depth.csv').write_text('existing,observation\n')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with patch.object(collect_snapshot, 'HERE', tmp), patch.object(collect_snapshot, 'run_puller') as puller, patch('sys.argv', ['collect_snapshot.py', '--date', day]):
                collect_snapshot.main()
            puller.assert_not_called()
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_partial_proxy_is_archived_before_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = '2026-11-02'
            output = Path(tmp) / 'github-output'
            def partial(script, workdir, key, extra=()):
                cg = Path(workdir) / 'cg_data'
                cg.mkdir(parents=True, exist_ok=True)
                (cg / 'cg_fiat_pairs_depth.csv').write_text('fixture,only\n')
                return {'script': script, 'returncode': 1, 'seconds': 0}
            with patch.object(collect_snapshot, 'HERE', tmp), patch.object(collect_snapshot, 'preflight'), patch.object(collect_snapshot, 'bootstrap_seed'), patch.object(collect_snapshot, 'sha256', return_value='fixture-hash'), patch.object(collect_snapshot, 'run_puller', side_effect=partial), patch.object(collect_snapshot.subprocess, 'run'), patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), patch('sys.argv', ['collect_snapshot.py', '--date', day]):
                with self.assertRaisesRegex(SystemExit, 'incomplete'):
                    collect_snapshot.main()
            snap = Path(tmp) / 'data' / 'snapshots' / day
            self.assertTrue((snap / 'cg_fiat_pairs_depth.csv').exists())
            self.assertTrue((snap / 'manifest.json').exists())
            self.assertEqual(output.read_text().strip(), f'snapshot_path={snap}')

    def test_workflow_stops_dependent_steps_after_failed_publication(self):
        workflow = (Path(__file__).parent / '.github/workflows/main.yml').read_text()
        proxy = workflow.split('- name: Collect CoinGecko proxy snapshot', 1)[1].split('- name:', 1)[0]
        commit = workflow.split('- name: Commit proxy snapshot', 1)[1]
        native = workflow.split('- name: Commit native data and coverage', 1)[1].split('- name:', 1)[0]
        self.assertIn("steps.native_publish.outcome == 'success'", proxy)
        self.assertIn("steps.proxy.outcome == 'success'", commit)
        self.assertIn("steps.native_panel.outcome == 'success'", native)
        self.assertIn('ref: main', workflow)
        self.assertEqual(workflow.count('commit_collected_data.sh --branch main'), 2)
        self.assertIn("steps.proxy.outputs.snapshot_path != ''", workflow)
        self.assertNotIn('continue-on-error:', workflow)


if __name__ == '__main__':
    unittest.main()
