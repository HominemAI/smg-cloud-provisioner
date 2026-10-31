# SMG Cloud Provisioner

A Kubernetes GPU node provisioner for hybrid K3s and Google Compute Engine environments, published by HOMINEM. It polls workload demand, creates GCE instances from an existing template, and removes idle capacity after a delay. Tailscale credentials and a supplied startup script connect new instances to the cluster.

## Components

| File | Responsibility |
| --- | --- |
| `provisioner.py` | Scaling loop, provisioning callbacks, and unready-node cleanup |
| `k8s.py` | GPU pod discovery and Kubernetes node lifecycle |
| `gce.py` | GCE instance creation and deletion |
| `cloud_provider.py` | Provider interface and startup-script substitution |
| `tailscale_provider.py` | Tailscale authentication support |
| `main.py` | Process startup and scaler loop |

GCE is the only implemented cloud provider. The provider interface is an extension point, not multi-cloud support.

## Configuration and execution

See [ENV.md](ENV.md) for environment variables. The process expects in-cluster Kubernetes credentials, Google Cloud credentials, an existing GCE instance template, and a startup script that joins the node to your cluster. Credentials and cluster-specific manifests belong outside Git.

```sh
docker build -t smg-cloud-provisioner .
```

Deploy the image with your own Kubernetes service account, RBAC, secret references, and environment configuration. Importing the application starts infrastructure clients; this is not a local dry-run command. No HTTP API is currently implemented.

## Current limitations

- Scaling compares GPU pod count with node count, assuming one GPU workload per node; it does not perform GPU-capacity bin packing.
- `SCALER_MIN_NODES` and `SCALER_MAX_NODES` are declared but not enforced by the current scaling loop.
- Unready-node cleanup uses node age, not elapsed time since the Ready condition changed.
- The GCE template region is currently fixed to `us-west1`.
- Recovery, scale-down behavior, and provisioning latency need validation for each deployment. This release does not claim production hardening or published performance measurements.

## AI Disclosure
AI was used to assist in the creation of this code. The model did not design the architecture
and is responsible for less than 5% of the code. All code was reviewed and tested by me.

- Assisted in writing less than 5% of the code including some helper functions.
- Generating some of the docs and collecting env vars/cataloging them.
- Reviewing code for potential bugs and improvements. Improvements were implemented by human.
- A rubber duck to go over my ideas with and get feed back on my approach and understanding.
- Generated most things above this Disclosure, I like writing docs, but I mean genAI is badass.
