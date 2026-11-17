# Environment Variables

## Core Infrastructure (gce.py)
- `GCP_PROJECT_ID` - **Required** - Google Cloud Project ID
- `GCP_INSTANCE_TEMPLATE_NAME` - **Required** - GCE instance template name
- `GCP_ZONE` - **Required** - GCP zone (e.g., "us-west1-a")
- `GCP_STARTUP_SCRIPT` - **Required** - Instance startup script template
- `INSTANCE_NAME` - Instance name prefix (default: "smgcp")

## Tailscale Provider (tailscale_provider.py)
- `TAILNET` - **Required** - Tailscale tailnet name
- `TS_OAUTH_ID` - **Required** - Tailscale OAuth client ID
- `TS_OAUTH_SECRET` - **Required** - Tailscale OAuth client secret
- `TS_AUTHKEY` - **Auto-generated** - Tailscale auth key (set by provider)

## Scaling Configuration (provisioner.py)
- `SCALER_TIME_DELAY` - Scaling check interval in seconds (default: "120")
- `SCALER_MAX_NODES` - Maximum number of nodes (default: "2")
- `SCALER_MIN_NODES` - Minimum number of nodes (default: "0")
- `SCALER_PROVISIONING_TIMEOUT` - Instance provisioning timeout in seconds (default: "300")
- `SCALER_DROP_DELAY` - Delay before dropping nodes in seconds (default: "900")
- `UNREADY_NODE_TIMEOUT` - Timeout for unready nodes in seconds (default: "600")

## Kubernetes Labels (k8s.py)
- `NODE_ROLE_LABEL` - Node label key for ownership (default: "smgp-role")
- `NODE_ROLE_LABEL_VALUE` - Node label value for role (default: "gpu")
- `INSTANCE_PROVIDER_LABEL` - Label key for instance provider (default: "smgp-instance-provider")
- `INSTANCE_IDENTIFIER_LABEL` - Label key for instance identifier (default: "smgp-instance-identifier")

## Dynamic Variables (startup script substitution)
Any variables in the startup script template using `__VARIABLE_NAME__` format will be automatically detected and require corresponding environment variables to be set.

## Emergency standard-node pool

- `GPU_ENABLED` — `true` by default. Set `false` for an emergency-only deployment.
- `EMERGENCY_ENABLED` — `false` by default.
- `EMERGENCY_PROVIDERS` — ordered comma-separated providers; default `ec2,gce`.
  Use `gce` for the initial live drill. Each configured provider requires its own
  credentials and template. `EMERGENCY_PROVIDER` is a single-provider compatibility alias.
- `EMERGENCY_INSTANCE_NAME` — `smg-emergency`; names include the provider suffix,
  for example `smg-emergency-gce-xxxxxxxx`. Pool prefixes must not overlap.
- `EMERGENCY_MAX_NODES` — `1`; zero disables new launches. The outage trigger
  launches only when no emergency instance exists, regardless of a larger cap.
- `EMERGENCY_TRIGGER_DELAY` — `300` seconds without an available worker.
- `EMERGENCY_RECOVERY_DELAY` — `900` seconds continuously recovered before drain.
- `EMERGENCY_STARTUP_SCRIPT` — required when enabled; use the contents of
  `scripts/emergency-startup.sh`. Supports `__ENV__` and `{{DYNAMIC}}` substitutions.
- `EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME` — required for GCE; standard CPU template.
- `EMERGENCY_GCP_ZONE` — defaults to `GCP_ZONE`.
- `AWS_REGION`, `EC2_LAUNCH_TEMPLATE` — required for EC2. Template accepts a name or ID.
- `EC2_LAUNCH_TEMPLATE_VERSION` — `$Default` by default; resolved to a numeric version per launch.
- `AWS_SUBNET_IDS` — required for EC2: comma-separated subnet IDs in one VPC and
  `AWS_REGION`. Supply one per candidate AZ for regional capacity selection.
- `EC2_CAPACITY_TYPE` — `on-demand` by default; set `spot` for a Spot Fleet.
  This overrides the purchase choice at the Fleet level; remove market options
  from the launch template.
- AWS credentials follow boto3's standard credential chain: workload IAM role,
  or `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional `AWS_SESSION_TOKEN`.
- `DRY_RUN` — `false`; when true, read inventories and log decisions without
  cloud creation/deletion, node mutation, pod eviction, or Tailscale key creation.
- `INSTANCE_AUDIT_LOG` — optional append-only JSONL file. Mount persistent storage
  for live tests. Every launch request is recorded before submission, and successful
  EC2 launches also record the EC2 instance IDs. A ledger write failure blocks the request.

The standard startup script additionally requires `K3S_URL`, `K3S_VERSION`,
`K3S_KEY` and `TS_AUTHKEY` (supplied by the existing Tailscale provider).
`K3S_VERSION` must match the server. The script uses the existing label-key variables
and always sets role `emergency`, ownership `smg-cloud-provisioner=true`, and taint
`smgp-emergency=true:NoSchedule`. Only workloads explicitly tolerating this taint
can use the node. Toleration alone does not override other placement constraints.

## GHCR authentication on emergency nodes

- `GHCR_PAT` — required secret-backed environment variable for the emergency and GPU
  startup scripts. Use a GitHub PAT for `agentsanchez` with permission to pull the
  required packages. Never put its value in a ConfigMap or committed manifest.
- Configure a Kubernetes Secret or ExternalSecret and reference its `GHCR_PAT`
  key in the provisioner environment. Example secret-reference structures are
  in `tests/fixtures`; adapt the external store and source key to your deployment.

The script atomically writes `/etc/rancher/k3s/registries.yaml`, owned by root
with mode `0600`, before starting K3s. It configures the GHCR mirror and username
`agentsanchez`; shell tracing is disabled before writing the password.
The file is replaced on each execution. This installs credentials on newly
created nodes; it does not rotate credentials on existing nodes. After changing
Secret Manager, wait for synchronization and restart the provisioner to refresh
its environment. Existing nodes need a registry-file update and K3s restart.
The rendered bootstrap script contains the token in cloud startup metadata/user
data, just as it contains the existing join credentials; do not export or log it.

`scripts/gpu-startup.sh` contains the existing GPU bootstrap with the same registry
setup and shell tracing disabled. Supply it through `GCP_STARTUP_SCRIPT` and
set `K3S_URL` to your control-plane URL.


## EC2 Fleet launch requirements

The EC2 provider uses `CreateFleet(Type=instant)` instead of `RunInstances`.
Each request targets exactly one instance in one region, with all eligible
configured subnets as alternatives. Spot uses `price-capacity-optimized`;
on-demand uses `lowest-price`. An instant Fleet performs one allocation attempt
and does not independently maintain capacity or replace interrupted instances.
The scaler reconciles inventory and schedules subsequent attempts.

The launch template must specify a concrete instance type, compatible regional
AMI, storage, and VPC security-group IDs. Do not set an AZ, placement group,
capacity-reservation target, network-interface block or instance market options.
Set public-IP assignment on public subnets, or provide NAT for private subnets.
Subnet overrides select the AZ. The provider verifies that subnets share a VPC
and uses `DescribeInstanceTypeOfferings` to exclude unsupported AZs; this is not
an available-capacity guarantee.

Example (placeholder IDs, not active configuration):

```sh
AWS_REGION=us-west-2
EC2_LAUNCH_TEMPLATE=lt-your-template
EC2_LAUNCH_TEMPLATE_VERSION=\$Default
AWS_SUBNET_IDS=subnet-first-az,subnet-second-az,subnet-third-az
EC2_CAPACITY_TYPE=spot
```

Provisioner IAM permissions must include `ec2:CreateFleet`, `ec2:RunInstances`,
`ec2:CreateTags`, `ec2:DescribeLaunchTemplateVersions`, `ec2:DescribeSubnets`,
`ec2:DescribeInstanceTypeOfferings`, `ec2:DescribeInstances`, and scoped
`ec2:TerminateInstances`. An instance profile also requires `iam:PassRole`
for that role. Spot service-linked-role setup may require an administrator;
the provider does not create IAM roles. An instant Fleet does not require the
separate `AWSServiceRoleForEC2Fleet` role.

Rendered user data is supplied directly to Fleet as base64, with a 16 KiB
unencoded limit. No temporary launch templates or versions are created. The
ledger records the request token, Fleet ID, instance IDs, selected candidate
subnets and allocation errors, without user data or credentials. The Fleet
request tags its instances at creation so inventory reconciliation still works
after a controller restart. A successful instance is retained even if other AZs
report errors. Unexpected multiple-instance results are recorded and terminated.
Spot interruption behavior is termination; a non-Ready node whose cloud instance
has disappeared is cleaned up after the provisioning timeout so it cannot block
replacement. This is polling-based recovery, not proactive interruption draining.

References: [Instant Fleets](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instant-fleet.html),
[Fleet configuration](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/create-ec2-fleet.html),
[IAM prerequisites](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-fleet-prerequisites.html).

## Additional AWS H100 GPU pool

- `GPU_EC2_ENABLED`: defaults to `false`; independent of `GPU_ENABLED` and the CPU emergency pool.
- `GPU_EC2_LAUNCH_TEMPLATE`, `GPU_EC2_LAUNCH_TEMPLATE_VERSION`: GPU template and pinned version.
- `GPU_EC2_CAPACITY_TYPE`: `spot` (default) or `on-demand`; no automatic price-class fallback.
- `GPU_EC2_ROLE`: `gpu-aws` by default. Pending pods use `nodeSelector: {smgp-role: gpu-aws}`.
- `GPU_EC2_INSTANCE_NAME`: `smg-gpu-aws`; must not overlap other pools' prefixes.
- `GPU_EC2_MAX_NODES`: defaults to 1; minimum is zero.
- `GPU_EC2_PROVISIONING_TIMEOUT`, `GPU_EC2_UNREADY_TIMEOUT`: default 1200 seconds for GPU bootstrap.
- `GPU_EC2_STARTUP_SCRIPT`: optional inline script; defaults to packaged `scripts/gpu-ec2-startup.sh`.
- `AWS_REGION`, `AWS_SUBNET_IDS`: region and comma-separated subnets across AZs.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: dedicated operator credentials from Secret `smg-cloud-provisioner-aws`; never use the interactive root login.

AWS GPU nodes have `nvidia.com/gpu=gpu-aws:NoSchedule`. Selected workloads need that
NoSchedule toleration and the role selector. The existing NVIDIA device plugin
already tolerates this taint key. Unqualified pending GPU pods retain the original
GCE pool; running pods count against the role of their assigned node.
