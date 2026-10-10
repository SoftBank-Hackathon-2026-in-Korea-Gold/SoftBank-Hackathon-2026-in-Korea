#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VM as the CloudMorph control plane. Run as the deploy user.
# The VM's attached service account provides GCP credentials (no keys on disk).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
REGION="${GCP_REGION:-asia-northeast3}"
PROJECT="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"

sudo apt-get update -qq
sudo apt-get install -y -qq docker.io git curl jq python3 ca-certificates >/dev/null
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker >/dev/null
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
if ! command -v cloudflared >/dev/null; then
  curl -sSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cloudflared.deb >/dev/null
fi
command -v gcloud >/dev/null || { echo "gcloud missing (Ubuntu GCE images ship it)"; exit 1; }

gcloud config set project "$PROJECT" >/dev/null 2>&1
gcloud config set run/region "$REGION" >/dev/null 2>&1
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet >/dev/null 2>&1
[[ -f ~/.ssh/id_ed25519 ]] || ssh-keygen -t ed25519 -N "" -C "cloudmorph-control" -f ~/.ssh/id_ed25519 >/dev/null
[[ -d ~/cloudmorph ]] || git clone -q https://github.com/SoftBank-Hackathon-2026-in-Korea-Gold/SoftBank-Hackathon-2026-in-Korea ~/cloudmorph
mkdir -p ~/cloudmorph-web
echo "bootstrap done. node-pool public key (add to node VMs' ssh-keys metadata):"
cat ~/.ssh/id_ed25519.pub
