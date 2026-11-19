#!/bin/bash
set -euo pipefail

echo "[INFO] Updating package lists (ignore dead repos)"
apt-get update -o Acquire::Retries=3 || true
apt-get install -y curl gnupg2 lsb-release

# Install Tailscale
echo "[INFO] Installing Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable --now tailscaled

echo "[INFO] Authenticating into tailnet"
tailscale up --authkey=__TS_AUTHKEY__ --hostname="$(hostname)"

echo "[INFO] Waiting for Tailscale to assign an IP"
until tailscale ip --4 >/tmp/ts-ip 2>/dev/null; do
  sleep 2
done
AGENT_IP=$(cat /tmp/ts-ip)
echo "[INFO] Tailscale IP detected: $AGENT_IP"
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

export INSTALL_K3S_VERSION="__K3S_VERSION__"


# PROVIDER_GPU_PREPARE

until nvidia-smi &> /dev/null && [ -e /dev/nvidia0 ]; do
  echo "[INFO] Waiting for GPU to be available..."
  sleep 2
done

# Install k3s agent
echo "[INFO] Installing k3s agent"
  curl -sfL https://get.k3s.io | \
    K3S_TOKEN="__K3S_KEY__" \
    sh -s - agent \
      --server __K3S_URL__ \
      --data-dir="$LOCAL_DISK/k3s" \
      --default-runtime=nvidia \
      --node-external-ip="$AGENT_IP" \
      --bind-address="$AGENT_IP" \
      --node-ip="$AGENT_IP" \
      --node-label __INSTANCE_IDENTIFIER_LABEL__={{INSTANCE_NAME}} \
      --node-label __INSTANCE_PROVIDER_LABEL__={{INSTANCE_PROVIDER}} \
      --node-label smg-cloud-provisioner=true \
      --node-label __NODE_ROLE_LABEL__=__GPU_ROLE__ \
      --node-label nvidia.com/gpu.present=true \
      --node-taint nvidia.com/gpu=__GPU_ROLE__:NoSchedule

# K3s detects nvidia-container-runtime from PATH and uses it by default.
echo '[INFO] GPU bootstrap completed'
