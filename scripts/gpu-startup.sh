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


# Install NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
export NVIDIA_CONTAINER_TOOLKIT_VERSION=__NVIDIA_CONTAINER_TOOLKIT_VERSION__
sudo apt-get install -y \
  nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
  libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}

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
      --node-external-ip="$AGENT_IP" \
      --bind-address="$AGENT_IP" \
      --node-ip="$AGENT_IP" \
      --node-label __INSTANCE_IDENTIFIER_LABEL__={{INSTANCE_NAME}} \
      --node-label __INSTANCE_PROVIDER_LABEL__={{INSTANCE_PROVIDER}} \
      --node-label smg-cloud-provisioner=true \
      --node-label __NODE_ROLE_LABEL__=__NODE_ROLE_LABEL_VALUE__ \
      --node-label nvidia.com/gpu.present=true

# Update k3s containerd config to use nvidia runtime
sudo nvidia-ctk runtime configure --runtime=containerd --config=/var/lib/rancher/k3s/agent/etc/containerd/config.toml --set-as-default
cp /var/lib/rancher/k3s/agent/etc/containerd/config.toml /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
sudo systemctl restart k3s-agent
