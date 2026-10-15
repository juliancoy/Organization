#!/usr/bin/env bash
set -euo pipefail
script="$(dirname -- "${BASH_SOURCE[0]}")/CodeCollective/scripts/push-all.sh"
if [[ ! -f "$script" ]]; then
  printf 'Run ./pull-all first to clone CodeCollective.\n' >&2
  exit 1
fi
exec bash "$script" "$@"
