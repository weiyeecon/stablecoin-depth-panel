"""Offline publication integration tests using real local bare Git remotes."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("publisher", Path(__file__).parent / "scripts/publish_collected_data.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.seed = self.clone("seed")
        self.write(self.seed, "data/exact_l2/old/book.json", "old native")
        self.write(self.seed, "data/snapshots/old/book.json", "old proxy")
        builder = '''from pathlib import Path
root=Path(__file__).parent
out=root/'panel';out.mkdir(exist_ok=True)
names=sorted(str(p.relative_to(root)) for p in (root/'data'/'ROOT').rglob('*') if p.is_file())
(out/'OUTPUT.csv').write_text('\\n'.join(names)+'\\n')
'''
        self.write(self.seed, "build_exact_l2_panel.py", builder.replace("ROOT", "exact_l2").replace("OUTPUT", "native"))
        self.write(self.seed, "build_panel.py", builder.replace("ROOT", "snapshots").replace("OUTPUT", "proxy"))
        self.write(self.seed, "panel/native.csv", "data/exact_l2/old/book.json\n")
        self.write(self.seed, "panel/proxy.csv", "data/snapshots/old/book.json\n")
        self.commit_push(self.seed, "initial")
        self.collector = self.clone("collector")

    def clone(self, name):
        path = self.root / name
        subprocess.run(["git", "clone", str(self.remote), str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(path, "config", "user.name", "test")
        git(path, "config", "user.email", "test@example.invalid")
        return path

    @staticmethod
    def write(repo, name, data):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    @staticmethod
    def commit_push(repo, message):
        git(repo, "add", ".")
        git(repo, "commit", "-m", message)
        git(repo, "push", "origin", "main")

    def test_stale_checkout_preserves_union_and_rebuilds_panels(self):
        original_head = git(self.collector, "rev-parse", "HEAD")
        self.write(self.seed, "data/exact_l2/upstream/book.json", "remote native")
        self.write(self.seed, "data/snapshots/upstream/book.json", "remote proxy")
        self.commit_push(self.seed, "new upstream observations")
        self.write(self.collector, "data/exact_l2/local/book.json", "local native")
        self.write(self.collector, "data/snapshots/local/book.json", "local proxy")
        # Stale derived files are intentionally not trusted or rebased.
        self.write(self.collector, "panel/native.csv", "wrong stale panel")
        result = publisher.publish(self.collector)
        fetched = self.clone("result")
        self.assertEqual(git(fetched, "rev-parse", "HEAD"), result)
        for kind, panel in (("exact_l2", "native"), ("snapshots", "proxy")):
            for name in ("old", "upstream", "local"):
                self.assertTrue((fetched / f"data/{kind}/{name}/book.json").exists())
                self.assertIn(f"data/{kind}/{name}/book.json", (fetched / f"panel/{panel}.csv").read_text())
        self.assertEqual(git(self.collector, "rev-parse", "HEAD"), original_head)
        self.assertEqual((self.collector / "panel/native.csv").read_text(), "wrong stale panel")
        self.assertFalse((self.collector / ".git/rebase-merge").exists())

    def test_concurrent_push_refetches_and_rebuilds(self):
        self.write(self.collector, "data/exact_l2/local/book.json", "local")
        real_git = publisher.git
        raced = []
        def race(repo, *args, **kwargs):
            if args[0] == "push" and not raced:
                raced.append(True)
                self.write(self.seed, "data/exact_l2/concurrent/book.json", "concurrent")
                self.commit_push(self.seed, "race")
            return real_git(repo, *args, **kwargs)
        with patch.object(publisher, "git", side_effect=race):
            publisher.publish(self.collector)
        fetched = self.clone("result")
        text = (fetched / "panel/native.csv").read_text()
        self.assertIn("data/exact_l2/concurrent/book.json", text)
        self.assertIn("data/exact_l2/local/book.json", text)
        self.assertEqual(len(raced), 1)

    def test_existing_raw_collision_fails_without_push_or_local_loss(self):
        before = git(self.seed, "rev-parse", "HEAD")
        self.write(self.collector, "data/exact_l2/old/book.json", "changed raw")
        with self.assertRaisesRegex(ValueError, "collision"):
            publisher.publish(self.collector)
        self.assertEqual(git(self.remote, "rev-parse", "main"), before)
        self.assertEqual((self.collector / "data/exact_l2/old/book.json").read_text(), "changed raw")

    def test_repeated_publication_is_noop(self):
        self.write(self.collector, "data/exact_l2/local/book.json", "local")
        first = publisher.publish(self.collector)
        self.assertEqual(publisher.publish(self.collector), first)
        self.assertEqual(git(self.remote, "rev-parse", "main"), first)

    def test_builder_failure_never_pushes_and_preserves_collecting_checkout(self):
        self.write(self.seed, "build_exact_l2_panel.py", "raise SystemExit(1)\n")
        self.commit_push(self.seed, "bad builder")
        before = git(self.remote, "rev-parse", "main")
        self.write(self.collector, "data/exact_l2/local/book.json", "local")
        with self.assertRaises(subprocess.CalledProcessError):
            publisher.publish(self.collector)
        self.assertEqual(git(self.remote, "rev-parse", "main"), before)
        self.assertTrue((self.collector / "data/exact_l2/local/book.json").exists())

    def test_native_only_publication_leaves_proxy_panel_unchanged(self):
        self.write(self.collector, "data/exact_l2/local/book.json", "local")
        publisher.publish(self.collector)
        fetched = self.clone("result")
        self.assertEqual((fetched / "panel/proxy.csv").read_bytes(),
                         (self.seed / "panel/proxy.csv").read_bytes())

    def test_permission_rejection_does_not_retry_or_change_upstream(self):
        self.write(self.collector, "data/exact_l2/local/book.json", "local")
        before = git(self.remote, "rev-parse", "main")
        real_git = publisher.git
        pushes = []
        def reject(repo, *args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, "", "permission denied")
            return real_git(repo, *args, **kwargs)
        with patch.object(publisher, "git", side_effect=reject):
            with self.assertRaisesRegex(RuntimeError, "push rejected without upstream advance"):
                publisher.publish(self.collector)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(git(self.remote, "rev-parse", "main"), before)
        self.assertTrue((self.collector / "data/exact_l2/local/book.json").exists())

    def test_symlink_data_is_rejected(self):
        (self.collector / "data/exact_l2/link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            publisher.publish(self.collector)


if __name__ == "__main__":
    unittest.main()
