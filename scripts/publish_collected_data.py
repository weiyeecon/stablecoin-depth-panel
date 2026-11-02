#!/usr/bin/env python3
"""Publish append-only observations against current upstream, never rebase panels.

The collecting checkout is deliberately left untouched. Each push candidate is
built in an isolated worktree so an unsuccessful push cannot strand the next
step in a rebase or replace previously published raw observations.
"""
import argparse
import filecmp
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

RAW_ROOTS = ("data/exact_l2", "data/snapshots")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def raw_files(repo):
    files = {}
    for root_name in RAW_ROOTS:
        root = repo / root_name
        if root.is_symlink() or root.parent.is_symlink():
            raise ValueError(f"refusing symlink: {root_name}")
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"refusing symlink: {path.relative_to(repo)}")
            if path.is_file():
                files[path.relative_to(repo).as_posix()] = path
    return files


def merge_raw(local_files, target):
    """Immutable-path union: identical data is idempotent; collisions stop."""
    added = []
    # Check every collision before adding anything, including symlink parents.
    raw_files(target)
    for name, source in local_files.items():
        destination = target / name
        if destination.exists():
            if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
                raise ValueError(f"raw observation collision; refusing overwrite: {name}")
        else:
            added.append(name)
    for name in added:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_files[name], destination)
    return added


def publish(repo, branch="main", attempts=3):
    repo = Path(repo).resolve()
    git(repo, "check-ref-format", f"refs/heads/{branch}")
    local_files = raw_files(repo)
    if not local_files:
        print("No local observations to publish.")
        return None
    for attempt in range(1, attempts + 1):
        git(repo, "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        base = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="depth-publish-") as temp:
            worktree = Path(temp) / "candidate"
            git(repo, "worktree", "add", "--detach", str(worktree), base)
            try:
                added = merge_raw(local_files, worktree)
                if not added:
                    print("All local observations already exist upstream; nothing to commit.")
                    return base
                native = any(name.startswith("data/exact_l2/") for name in added)
                proxy = any(name.startswith("data/snapshots/") for name in added)
                if native:
                    subprocess.run([sys.executable, "build_exact_l2_panel.py"],
                                   cwd=worktree, check=True)
                if proxy:
                    subprocess.run([sys.executable, "build_panel.py"],
                                   cwd=worktree, check=True)
                # Stage only observations and derived panels; no source changes.
                git(worktree, "add", "--", *added, "panel")
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                git(worktree, "-c", "user.name=depth-collector", "-c",
                    "user.email=depth-collector@users.noreply.github.com",
                    "commit", "-m", f"depth snapshot {day} [skip ci]")
                candidate = git(worktree, "rev-parse", "HEAD").stdout.strip()
                pushed = git(worktree, "push", "origin",
                             f"HEAD:refs/heads/{branch}", check=False)
                if pushed.returncode == 0:
                    remote = git(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
                    if not remote:
                        raise RuntimeError("push succeeded but remote branch could not be verified")
                    # Another legitimate writer may already have advanced main.
                    git(repo, "fetch", "--no-tags", "origin", f"refs/heads/{branch}")
                    git(repo, "merge-base", "--is-ancestor", candidate, "FETCH_HEAD")
                    print(f"Published {candidate} on attempt {attempt}; {len(added)} raw files added.")
                    return candidate
                # Only a concurrent branch advance warrants a retry. Permission,
                # policy, and transport failures remain visible and fail closed.
                remote = git(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
                if not remote or remote[0] == base:
                    raise RuntimeError(f"push rejected without upstream advance: {pushed.stderr.strip()}")
                print(f"Upstream advanced; rebuilding the raw union (attempt {attempt}).")
            finally:
                git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    raise RuntimeError(f"upstream kept advancing across {attempts} push attempts; original observations retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", "main"))
    args = parser.parse_args()
    try:
        publish(args.repo_root, args.branch)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Publication failed; collecting checkout and artifacts retained: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
