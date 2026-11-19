"""Assemble provider hooks around shared registry, Tailscale and k3s bootstrap."""
from pathlib import Path
import re


def gpu_bootstrap(hook, role):
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,61}[a-z0-9]|[a-z0-9]', role):
        raise ValueError('GPU role must be a valid lowercase Kubernetes label value')
    root = Path(__file__).parent / 'scripts'
    return (root / 'gpu-common-startup.sh').read_text().replace(
        '# PROVIDER_GPU_PREPARE', (root / hook).read_text()).replace('__GPU_ROLE__', role)
