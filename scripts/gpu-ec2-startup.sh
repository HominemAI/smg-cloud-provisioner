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


# The AWS GPU base AMI supplies a compatible driver and container runtime.
command -v nvidia-container-runtime
if systemctl list-unit-files amazon-ssm-agent.service --no-legend | grep -q amazon-ssm-agent; then
  systemctl enable --now amazon-ssm-agent
else
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
fi

# Use only local instance-store NVMe, never an EBS NVMe root disk.
LOCAL_DISK=""
if mountpoint -q /opt/dlami/nvme; then
  source_disk=$(findmnt -n -o SOURCE -M /opt/dlami/nvme)
  lsblk -s -n -o MODEL "$source_disk" | grep -q 'Amazon EC2 NVMe Instance Storage'
  LOCAL_DISK=/opt/dlami/nvme
else
  mapfile -t disks < <(lsblk -dn -o PATH,MODEL | awk '$0 ~ /Amazon EC2 NVMe Instance Storage/ {print $1}')
  if [ "${#disks[@]}" -ne 1 ]; then
    echo '[ERROR] Expected one instance-store disk or the DLAMI NVMe mount' >&2
    exit 1
  fi
  disk="${disks[0]}"
  # Refuse partitioned disks or unexpected mounts instead of destroying data.
  [ "$(lsblk -nr -o NAME "$disk" | wc -l)" -eq 1 ]
  existing_mount=$(findmnt -n -o TARGET -S "$disk" || true)
  if [ -n "$existing_mount" ]; then
    LOCAL_DISK="$existing_mount"
  else
    fs_type=$(blkid -s TYPE -o value "$disk" || true)
    if [ -z "$fs_type" ]; then
      [ -z "$(wipefs -n --noheadings "$disk")" ]
      mkfs.ext4 -L smg-local "$disk"
    elif [ "$fs_type" != ext4 ] && [ "$fs_type" != xfs ]; then
      echo '[ERROR] Unexpected instance-store filesystem' >&2
      exit 1
    fi
    LOCAL_DISK=/mnt/smg-local
    mkdir -p "$LOCAL_DISK"
    disk_uuid=$(blkid -s UUID -o value "$disk")
    if ! grep -q "UUID=$disk_uuid " /etc/fstab; then
      printf 'UUID=%s %s auto defaults 0 2\n' "$disk_uuid" "$LOCAL_DISK" >> /etc/fstab
    fi
    mount "$LOCAL_DISK"
  fi
fi
mountpoint -q "$LOCAL_DISK"
mkdir -p "$LOCAL_DISK/k3s" "$LOCAL_DISK/models"
ln -sfn "$LOCAL_DISK/models" /mnt/smg-models
# Fail closed on subsequent boots if the ephemeral disk did not mount.
mkdir -p /etc/systemd/system/k3s-agent.service.d
cat > /etc/systemd/system/k3s-agent.service.d/storage.conf <<STORAGE
[Unit]
RequiresMountsFor=$LOCAL_DISK
[Service]
ExecStartPre=/usr/bin/mountpoint -q $LOCAL_DISK
STORAGE

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
      --node-label __NODE_ROLE_LABEL__=__GPU_EC2_ROLE__ \
      --node-label nvidia.com/gpu.present=true \
      --node-taint nvidia.com/gpu=__GPU_EC2_ROLE__:NoSchedule

# K3s detects nvidia-container-runtime from PATH and uses it by default.
echo '[INFO] AWS GPU bootstrap completed'
