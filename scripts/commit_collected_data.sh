#!/usr/bin/env bash
# Preserve every collected artifact even when some markets fail.
set -euo pipefail
git config user.name "depth-collector"
git config user.email "depth-collector@users.noreply.github.com"
git add -A data/exact_l2 data/snapshots panel
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "depth snapshot $(date -u +%F) [skip ci]"
for attempt in 1 2 3; do
  if ! git pull --rebase origin "${GITHUB_REF_NAME}"; then
    echo "Rebase failed; refusing to push over concurrent changes."
    exit 1
  fi
  if git push origin "HEAD:${GITHUB_REF_NAME}"; then
    echo "pushed on attempt ${attempt}"
    exit 0
  fi
  sleep 5
done
echo "push failed after 3 attempts"
exit 1
