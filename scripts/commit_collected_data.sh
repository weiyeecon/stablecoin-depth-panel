#!/usr/bin/env bash
# Publish an immutable raw-data union against current upstream; never rebase CSVs.
set -euo pipefail
python "$(dirname "$0")/publish_collected_data.py" "$@"
