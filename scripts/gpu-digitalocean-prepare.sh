# Pinned NVIDIA AI/ML image must provide a tested driver and container runtime.
# Use the persistent boot disk. Never format or assume the identity of scratch disks.
command -v nvidia-container-runtime
nvidia-smi
LOCAL_DISK=/var/lib/smg-gpu
install -d -m 0755 "$LOCAL_DISK/k3s" "$LOCAL_DISK/models"
if [ -e /mnt/smg-models ] && [ ! -L /mnt/smg-models ]; then
  echo '[ERROR] Existing model directory requires operator migration' >&2
  exit 1
fi
ln -sfn "$LOCAL_DISK/models" /mnt/smg-models
