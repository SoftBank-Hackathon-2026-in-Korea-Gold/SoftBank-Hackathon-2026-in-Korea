#!/usr/bin/env bash
# Stop everything started by demo-up.sh (and any leftover deployer containers/tunnels from the demo).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; STATE="$ROOT/.demo"
for name in tunnel frontend backend; do
  f="$STATE/$name.pid"
  if [[ -f "$f" ]]; then pid=$(cat "$f"); pkill -P "$pid" 2>/dev/null; kill "$pid" 2>/dev/null && echo "stopped $name ($pid)"; rm -f "$f"; fi
done
pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:${FRONT_PORT:-5173}" 2>/dev/null
# drop the push webhooks that pointed at this run's (now dead) tunnel
if [[ -n "${CLOUDMORPH_WEBHOOK_REPOS:-}" ]] && command -v gh >/dev/null; then
  IFS=',' read -ra REPOS <<< "$CLOUDMORPH_WEBHOOK_REPOS"
  for repo in "${REPOS[@]}"; do
    repo="$(echo "$repo" | xargs)"; [[ -z "$repo" ]] && continue
    for id in $(gh api "repos/$repo/hooks" --jq '.[] | select(.config.url | test("trycloudflare\\.com/webhook/github")) | .id' 2>/dev/null); do
      gh api -X DELETE "repos/$repo/hooks/$id" >/dev/null 2>&1 && echo "removed webhook $id on $repo"
    done
  done
fi
rm -f "$STATE/public_url" "$STATE/token"
# deployer-made local containers follow the "<slug>-local-<timestamp>" naming
ids=$(docker ps -aq --filter "name=-local-20" 2>/dev/null); [[ -n "$ids" ]] && docker rm -f $ids >/dev/null && echo "removed deployer containers"
pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:" 2>/dev/null && echo "stopped deployer tunnels"
echo "done"
