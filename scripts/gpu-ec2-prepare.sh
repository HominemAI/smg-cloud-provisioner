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
