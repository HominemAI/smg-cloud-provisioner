#!/bin/bash
# Debian/Ubuntu standard node, based on the existing infrastructure GPU script.
set -euo pipefail
apt-get update -o Acquire::Retries=3
apt-get install -y curl gnupg2 lsb-release
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled
tailscale up --authkey='__TS_AUTHKEY__' --hostname='{{INSTANCE_NAME}}'
until tailscale ip --4 >/tmp/ts-ip 2>/dev/null; do sleep 2; done
AGENT_IP=$(cat /tmp/ts-ip)
# Install registry credentials before K3s/containerd starts. Do not trace secrets.
set +x
install -d -m 0755 /etc/rancher/k3s
(
  umask 077
  registry_tmp=$(mktemp /etc/rancher/k3s/registries.yaml.XXXXXX)
  trap 'rm -f "$registry_tmp"' EXIT
  cat > "$registry_tmp" <<'GHCR_REGISTRY_CONFIG'
mirrors:
  ghcr.io:
    endpoint:
      - "https://ghcr.io"
configs:
  ghcr.io:
    auth:
      username: "agentsanchez"
      password: "__GHCR_PAT__"
GHCR_REGISTRY_CONFIG
  chown root:root "$registry_tmp"
  chmod 0600 "$registry_tmp"
  mv -f "$registry_tmp" /etc/rancher/k3s/registries.yaml
)

export INSTALL_K3S_VERSION='__K3S_VERSION__'
curl -sfL https://get.k3s.io | K3S_TOKEN='__K3S_KEY__' sh -s - agent \
  --server '__K3S_URL__' \
  --node-name '{{INSTANCE_NAME}}' \
  --node-external-ip="$AGENT_IP" --bind-address="$AGENT_IP" --node-ip="$AGENT_IP" \
  --node-label '__INSTANCE_IDENTIFIER_LABEL__={{INSTANCE_NAME}}' \
  --node-label '__INSTANCE_PROVIDER_LABEL__={{INSTANCE_PROVIDER}}' \
  --node-label smg-cloud-provisioner=true \
  --node-label '__NODE_ROLE_LABEL__=emergency' \
  --node-taint 'smgp-emergency=true:NoSchedule'
