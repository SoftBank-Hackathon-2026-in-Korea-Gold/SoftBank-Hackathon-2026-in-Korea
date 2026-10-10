#!/usr/bin/env bash
# Bring up CloudMorph for the demo on this laptop:
#   backend (uvicorn :8000) + dashboard (vite :5173, proxies /deploy & /health to the backend)
#   --public : also open ONE Cloudflare quick tunnel to the dashboard and require an API token.
# Usage: scripts/demo-up.sh [--public]        Stop: scripts/demo-down.sh
#   CLOUDMORPH_WEBHOOK_REPOS=owner/repo[,owner/repo2]  with --public: (re)register each repo's GitHub push webhook
#   to this run's tunnel URL (quick-tunnel URLs change every run). Needs `gh auth login`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/.demo"; mkdir -p "$STATE"
PUBLIC=0; [[ "${1:-}" == "--public" ]] && PUBLIC=1
BACKEND_PORT="${BACKEND_PORT:-8000}"; FRONT_PORT="${FRONT_PORT:-5173}"

say() { printf '\033[1;36m[demo]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[demo] %s\033[0m\n' "$*" >&2; exit 1; }

# ---- preflight ---------------------------------------------------------------
[[ -f "$ROOT/.env" ]] || die ".env not found. cp .env.example .env and fill GCP_PROJECT_ID / ANTHROPIC_API_KEY"
set -a; source "$ROOT/.env"; set +a
[[ -n "${ANTHROPIC_API_KEY:-}" ]] || die "ANTHROPIC_API_KEY is empty in .env (analyzer needs it; OPENAI_API_KEY alone is not enough)"
command -v docker >/dev/null || die "docker not installed"
docker info >/dev/null 2>&1 || die "Docker daemon not running (start Docker Desktop)"
command -v uv >/dev/null || die "uv not installed"
command -v npm >/dev/null || die "npm not installed"
command -v cloudflared >/dev/null || die "cloudflared not installed (brew install cloudflared)"
if command -v gcloud >/dev/null && [[ -n "${GCP_PROJECT_ID:-}" ]]; then
  gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q . || die "no active gcloud account (gcloud auth login)"
  [[ "$(gcloud billing projects describe "$GCP_PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null)" == "True" ]] || die "billing not enabled on $GCP_PROJECT_ID"
  say "gcloud ok: project $GCP_PROJECT_ID"
else
  say "WARNING: gcloud/GCP_PROJECT_ID missing -> cloudrun target will fail preflight (local still works)"
fi
for port in "$BACKEND_PORT" "$FRONT_PORT"; do
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && die "port $port already in use (scripts/demo-down.sh or free it)"
done
[[ -d "$ROOT/frontend/node_modules" ]] || { say "npm install (first run)"; (cd "$ROOT/frontend" && npm install --no-audit --no-fund >/dev/null); }

WEBHOOK_REPOS="${CLOUDMORPH_WEBHOOK_REPOS:-}"
if [[ $PUBLIC -eq 1 && -n "$WEBHOOK_REPOS" ]]; then
  command -v gh >/dev/null || die "gh CLI not installed (needed for CLOUDMORPH_WEBHOOK_REPOS)"
  export CLOUDMORPH_WEBHOOK_SECRET="${CLOUDMORPH_WEBHOOK_SECRET:-$(openssl rand -hex 16)}"
fi

# ---- token (only when public) -----------------------------------------------
if [[ $PUBLIC -eq 1 ]]; then
  export CLOUDMORPH_API_TOKEN="${CLOUDMORPH_API_TOKEN:-$(openssl rand -hex 12)}"
  echo "$CLOUDMORPH_API_TOKEN" > "$STATE/token"; chmod 600 "$STATE/token"
else
  unset CLOUDMORPH_API_TOKEN
fi

# ---- start -------------------------------------------------------------------
say "backend  -> http://127.0.0.1:$BACKEND_PORT"
(cd "$ROOT/backend" && nohup uv run uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --workers 1 \
   > "$STATE/backend.log" 2>&1 & echo $! > "$STATE/backend.pid")
say "frontend -> http://127.0.0.1:$FRONT_PORT"
(cd "$ROOT/frontend" && VITE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" nohup npm run dev -- --host 127.0.0.1 --port "$FRONT_PORT" \
   > "$STATE/frontend.log" 2>&1 & echo $! > "$STATE/frontend.pid")
for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null || die "backend did not come up, see $STATE/backend.log"
for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$FRONT_PORT/health" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$FRONT_PORT/health" >/dev/null || die "frontend proxy not answering, see $STATE/frontend.log"
say "local stack is up (dashboard proxies the API)"

if [[ $PUBLIC -eq 1 ]]; then
  nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$FRONT_PORT" > "$STATE/tunnel.log" 2>&1 &
  echo $! > "$STATE/tunnel.pid"
  URL=""
  for i in $(seq 1 60); do URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$STATE/tunnel.log" | head -n 1 || true); [[ -n "$URL" ]] && break; sleep 1; done  # `|| true`: under set -e a miss would silently kill the script
  [[ -n "$URL" ]] || die "tunnel URL not found, see $STATE/tunnel.log"
  for i in $(seq 1 30); do grep -q "Registered tunnel connection" "$STATE/tunnel.log" && break; sleep 1; done
  echo "$URL" > "$STATE/public_url"
  if [[ -n "$WEBHOOK_REPOS" ]]; then
    IFS=',' read -ra REPOS <<< "$WEBHOOK_REPOS"
    for repo in "${REPOS[@]}"; do
      repo="$(echo "$repo" | xargs)"; [[ -z "$repo" ]] && continue
      for old in $(gh api "repos/$repo/hooks" --jq '.[] | select(.config.url | test("trycloudflare\\.com/webhook/github")) | .id' 2>/dev/null); do
        gh api -X DELETE "repos/$repo/hooks/$old" >/dev/null 2>&1 || true
      done
      body=$(printf '{"name":"web","active":true,"events":["push"],"config":{"url":"%s/webhook/github","content_type":"json","secret":"%s","insecure_ssl":"0"}}' "$URL" "$CLOUDMORPH_WEBHOOK_SECRET")
      if hook=$(printf '%s' "$body" | gh api -X POST "repos/$repo/hooks" --input - --jq '.id' 2>"$STATE/webhook.err"); then
        say "webhook          : $repo -> $URL/webhook/github (id $hook)"
      else
        say "WARNING: webhook registration failed for $repo: $(tail -n 1 "$STATE/webhook.err")"
      fi
    done
  fi
  echo
  say "PUBLIC dashboard : $URL   (DNS may take ~1 min to propagate)"
  say "API token        : $CLOUDMORPH_API_TOKEN   (header X-API-Token, or ?token= for SSE)"
  say "example: curl -X POST $URL/deploy -H 'X-API-Token: $CLOUDMORPH_API_TOKEN' -H 'Content-Type: application/json' -d '{\"source\":\"https://github.com/<org>/<repo>\",\"targets\":[\"local\",\"cloudrun\"]}'"
fi
say "logs: $STATE/{backend,frontend,tunnel}.log   stop: scripts/demo-down.sh"
