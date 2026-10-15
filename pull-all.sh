#!/usr/bin/env bash
# Clone missing workspace repositories; fast-forward existing checkouts.
set -uo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="${1:-$script_dir}"
if (( $# > 1 )) || [[ ! -d "$root" ]]; then
  printf 'Usage: %s [directory]\n' "$0" >&2
  exit 2
fi
total=0
failed=0
while read -r name url extra; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  total=$((total + 1))
  repo="$root/$name"
  printf '\n==> %s\n' "$repo"
  if [[ -n "$extra" || -z "$url" || ! "$name" =~ ^[A-Za-z0-9_-]+$ ]]; then
    printf 'Invalid repository entry: %s\n' "$name" >&2
    failed=$((failed + 1))
    continue
  fi
  if [[ -d "$repo" && -e "$repo/.git" ]]; then
    # Never stash, reset, merge, or rewrite local work automatically.
    if git -C "$repo" pull --ff-only; then continue; fi
  elif [[ -e "$repo" || -L "$repo" ]]; then
    printf 'Refusing to replace an existing non-repository: %s\n' "$repo" >&2
  elif git clone -- "$url" "$repo"; then
    continue
  fi
  printf 'Failed: %s\n' "$name" >&2
  failed=$((failed + 1))
done < "$script_dir/repositories.txt"
printf '\nRepositories: %d; successful: %d; failed: %d\n' \
  "$total" "$((total - failed))" "$failed"
(( failed == 0 ))
