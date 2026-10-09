#!/usr/bin/env bash
# Sync GitHub labels from .github/labels.yml (idempotent).
#
#   scripts/sync-labels.sh            # apply to the current repo
#   DRY_RUN=1 scripts/sync-labels.sh  # print actions only
#
# Requires: gh (authenticated), yq v4, jq.
set -euo pipefail

LABELS_FILE="$(git rev-parse --show-toplevel)/.github/labels.yml"
DRY_RUN="${DRY_RUN:-0}"

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; echo
  else
    "$@"
  fi
}

for cmd in gh yq jq; do
  command -v "$cmd" >/dev/null || { echo "missing dependency: $cmd" >&2; exit 1; }
done
[[ -f "$LABELS_FILE" ]] || { echo "not found: $LABELS_FILE" >&2; exit 1; }

existing="$(gh label list --limit 500 --json name -q '.[].name')"
has_label() { grep -Fxq -- "$1" <<<"$existing"; }

# 1) create / update / rename
while IFS= read -r label; do
  name="$(jq -r '.name' <<<"$label")"
  color="$(jq -r '.color' <<<"$label")"
  desc="$(jq -r '.description // ""' <<<"$label")"

  if has_label "$name"; then
    echo "update  $name"
    run gh label edit "$name" --color "$color" --description "$desc"
    continue
  fi

  renamed=0
  while IFS= read -r alias; do
    [[ -z "$alias" ]] && continue
    if has_label "$alias"; then
      echo "rename  $alias -> $name"
      run gh label edit "$alias" --name "$name" --color "$color" --description "$desc"
      renamed=1
      break
    fi
  done < <(jq -r '.aliases // [] | .[]' <<<"$label")

  if [[ "$renamed" == "0" ]]; then
    echo "create  $name"
    run gh label create "$name" --color "$color" --description "$desc" --force
  fi
done < <(yq -o=json -I=0 '.labels[]' "$LABELS_FILE")

# 2) delete obsolete labels
while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  if has_label "$name"; then
    echo "delete  $name"
    run gh label delete "$name" --yes
  fi
done < <(yq -r '.delete // [] | .[]' "$LABELS_FILE")

echo "done."
