# Environment Variables

## Core Infrastructure (gce.py)
- `GCP_PROJECT` - **Required** - Google Cloud Project ID
- `INSTANCE_TEMPLATE` - **Required** - GCE instance template name
- `GCP_ZONE` - GCP zone (default: "us-west1-a")
- `INSTANCE_NAME` - Instance name prefix (default: "ts-node")
- `STARTUP_SCRIPT` - Instance startup script template (default: echo message)

## Tailscale Provider (tailscale_provider.py)
- `TAILNET` - **Required** - Tailscale tailnet name
- `TS_OAUTH_ID` - **Required** - Tailscale OAuth client ID
- `TS_OAUTH_SECRET` - **Required** - Tailscale OAuth client secret
- `TS_AUTHKEY` - **Auto-generated** - Tailscale auth key (set by provider)

## Scaling Configuration (provisioner.py)
- `SCALER_TIME_DELAY` - Scaling check interval in seconds (default: "60")
- `SCALER_MAX_NODES` - Maximum number of nodes (default: "2")
- `SCALER_MIN_NODES` - Minimum number of nodes (default: "0")
- `SCALER_PROVISIONING_TIMEOUT` - Instance provisioning timeout in seconds (default: "300")
- `SCALER_DROP_DELAY` - Delay before dropping nodes in seconds (default: "300")

## K3s Configuration
- `K3S_KEY` - **Required** - K3s cluster join token (used in main.py and provisioner.py)

## Application Server (main.py)
- `PORT` - HTTP server port (default: 80)

## Kubernetes Labels (k8s.py)
- `NODE_OWNER_LABEL` - Node label key for ownership (default: "smgp-role")
- `NODE_ROLE_LABEL_VALUE` - Node label value for role (default: "gpu")
- `INSTANCE_PROVIDER_LABEL` - Label key for instance provider (default: "smgp-instance-provider")
- `INSTANCE_IDENTIFIER_LABEL` - Label key for instance identifier (default: "smgp-instance-identifier")

## Dynamic Variables (startup script substitution)
Any variables in the startup script template using `__VARIABLE_NAME__` format will be automatically detected and require corresponding environment variables to be set.