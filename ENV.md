# Environment Variables

Runtime configuration is read at process startup. Supply secrets through Kubernetes
Secrets/ExternalSecrets, and restart the provisioner after its environment changes.
Environment feature flags are enabled only when their value, lowercased, is `true`.
JSON flags must be actual booleans. Times below are seconds unless stated otherwise.

New provider configuration is covered in [configured GPU pools](#configured-gpu-pools),
[Azure credentials](#azure-credentials), [DigitalOcean credentials](#digitalocean-credentials),
[additional provider credentials](#scaleway-nebius-and-ovhcloud-credentials), and [local emulator settings](#local-emulator-and-test-environment).

## Core Infrastructure (gce.py)

Required only when a GCE pool is enabled; JSON provider options can supply the
project, zone, template and script instead of these environment variables.

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
- `GPU_EC2_LAUNCH_TEMPLATE`: required GPU launch-template name or ID when this pool is enabled.
- `GPU_EC2_LAUNCH_TEMPLATE_VERSION`: defaults to `$Default`; resolved to a numeric version for each launch.
- `GPU_EC2_CAPACITY_TYPE`: optional explicit `spot` or `on-demand`; otherwise template settings, then `EC2_CAPACITY_TYPE`/on-demand default.
- `GPU_EC2_ROLE`: `gpu-aws` by default. Pending pods use `nodeSelector: {smgp-role: gpu-aws}`.
- `GPU_EC2_INSTANCE_NAME`: `smg-gpu-aws`; must not overlap other pools' prefixes.
- `GPU_EC2_MAX_NODES`: defaults to 1; minimum is zero.
- `GPU_EC2_PROVISIONING_TIMEOUT`, `GPU_EC2_UNREADY_TIMEOUT`: default 1200 seconds for GPU bootstrap.
- `GPU_EC2_STARTUP_SCRIPT`: optional inline script; defaults to the shared GPU bootstrap with `scripts/gpu-ec2-prepare.sh`. The standalone EC2 script remains for compatibility.
- `AWS_REGION`, `AWS_SUBNET_IDS`: region and comma-separated subnets across AZs.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`: dedicated operator credentials from Secret `smg-cloud-provisioner-aws`; never use the interactive root login.

AWS GPU nodes have `nvidia.com/gpu=gpu-aws:NoSchedule`. Selected workloads need that
NoSchedule toleration and the role selector. The existing NVIDIA device plugin
already tolerates this taint key. Unqualified pending GPU pods retain the original
GCE pool; running pods count against the role of their assigned node.

## Configured GPU pools

`GPU_POOLS_JSON` is the new optional environment variable for selecting and
configuring GPU pools. Its value is a JSON **array**, not a filename. It has no
default value:

| Value | Behavior |
| --- | --- |
| Unset | Preserve legacy `GPU_ENABLED`/`GPU_EC2_ENABLED` pool construction and their settings. |
| `[]` | Construct no GPU pools. An emergency pool must be enabled for the main process to have any pool to run. |
| Nonempty JSON array | Replace legacy GPU pool construction with the explicitly enabled entries. |
| Empty string or invalid JSON | Configuration error; it does not fall back to legacy settings. |

`EMERGENCY_ENABLED` and all emergency settings remain independent. Providers are
constructed lazily: a disabled entry does not require credentials. There are no
`GPU_AZURE_ENABLED` or `GPU_DO_ENABLED` environment flags; use entries below.

For each array entry:

| JSON field | Required/default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Must be boolean `true` to construct the pool. |
| `name` | Required when enabled | Unique pool name; `emergency` is reserved. |
| `provider` | Required when enabled | `gce`, `ec2`, `azure`, `digitalocean`, `scaleway`, `nebius`, or `ovhcloud`. |
| `role` | Required when enabled | Kubernetes role-label value; must differ across pools. For example `gpu-azure` or `gpu-do`. |
| `max_nodes` | `1` | Nonnegative integer; `0` prevents new launches. Applies to aggregate capacity across all candidates. |
| `provisioning_timeout` | `1200` | Nonnegative integer; time allowed for a node to join before ordinary cleanup. Does not release an uncertain launch reservation. |
| `unready_timeout` | `1200` | Nonnegative integer used by the unready-node policy. |
| `options` | Required when enabled | Provider constructor options; see tables below. |

Configured GPU pools have minimum zero nodes, inherit `DRY_RUN`, and use
`SCALER_DROP_DELAY` (`900`) for idle scale-down. Their two timeout fields above
replace the global `SCALER_PROVISIONING_TIMEOUT`/`UNREADY_NODE_TIMEOUT` defaults.
Legacy `GPU_EC2_*` values do not fill JSON fields. Roles still control GPU demand
routing; adding a provider does not automatically redirect untargeted workloads.

Common Azure/DO `options` fields:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name_prefix` | Azure: `smg-gpu-azure`; DO: `smg-gpu-do` | 2–32 lowercase letters/digits/hyphens, starting with a letter and ending alphanumeric. Keep stable while resources exist. |
| `ledger_path` | Required; no default | Persistent SQLite file path. Parent directory must already exist and be writable. The examples use `/var/lib/smg-cloud-provisioner/lifecycle.sqlite`. |
| `startup_script` | Packaged shared GPU bootstrap plus provider preparation hook | Optional inline script override; not a filesystem path. An override must supply the correct role/taint and bootstrap behavior itself. |
| `settings` | Required | Azure or DigitalOcean settings object below. |

`ledger_path` is a JSON option, **not** an environment variable. It records durable
reservations and native resource IDs and is separate from `INSTANCE_AUDIT_LOG`
(the optional append-only JSONL event log). Persist and back up both. Neither
stores rendered bootstrap or credential values. Inventory reconciliation can
update local lifecycle metadata even with `DRY_RUN=true`; it does not create or
delete cloud resources. Run one active controller per owned pool.

For GCE/EC2 JSON entries, constructor options retain the existing fallbacks:

| Provider | `options` field → environment fallback |
| --- | --- |
| GCE | `project` → `GCP_PROJECT_ID`; `zone` → `GCP_ZONE`; `template` → `GCP_INSTANCE_TEMPLATE_NAME`; `startup_script` → `GCP_STARTUP_SCRIPT`; `name_prefix` → `INSTANCE_NAME` (`smgcp`). |
| EC2 | `region` → `AWS_REGION`; `template` → `EC2_LAUNCH_TEMPLATE`; `version` → `EC2_LAUNCH_TEMPLATE_VERSION` (`$Default`); `subnet_ids` → `AWS_SUBNET_IDS`; `capacity_type` → `EC2_CAPACITY_TYPE` (`on-demand`); `name_prefix` → `INSTANCE_NAME` (`smgcp`). |

EC2 JSON GPU pools receive the shared GPU bootstrap for their configured `role`
when no `startup_script` is supplied. GCE custom scripts must render their role
consistently; the legacy GCE script uses `NODE_ROLE_LABEL_VALUE`. Set an explicit
unique `name_prefix` for each configured pool.

## Azure credentials

| Environment variable | Required/default | Secret? | Purpose |
| --- | --- | --- | --- |
| `AZURE_TENANT_ID` | Required for the documented authentication modes; no default | No | Microsoft Entra tenant ID. |
| `AZURE_CLIENT_ID` | Required; no default | No | Runtime application/service-principal client ID. |
| `AZURE_CLIENT_SECRET` | Required for client-secret authentication; no default | **Yes** | Runtime service-principal secret. Omit when using workload federation. |
| `AZURE_FEDERATED_TOKEN_FILE` | Optional; unset by default | Path to a **secret** token file | A nonempty value selects `WorkloadIdentityCredential`. Mount a valid federated token at this path; tenant and client IDs remain required. |

When `AZURE_FEDERATED_TOKEN_FILE` is set, federation takes precedence over the
client-secret mode. Otherwise the provider uses the Azure SDK's
`EnvironmentCredential`; there is no Azure CLI login or implicit managed-identity
fallback. The SDK also recognizes certificate credentials
(`AZURE_CLIENT_CERTIFICATE_PATH`, optional secret
`AZURE_CLIENT_CERTIFICATE_PASSWORD`, and optional
`AZURE_CLIENT_SEND_CERTIFICATE_CHAIN`) when client-secret variables are absent.
Certificate authentication is SDK-provided and has not been live-qualified here;
use the documented secret or federation mode for initial validation.

### Azure JSON settings

These are fields in `options.settings`, **not** environment variables. In
particular, `AZURE_SUBSCRIPTION_ID` is not read by this provider.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `subscription_id` | Required | Target Azure subscription ID. |
| `resource_group` | Required | Existing dedicated VM resource group. |
| `location` | Required | Exact Azure region; no implicit region selection. |
| `subnet_id` | Required | Existing subnet ARM resource ID in the configured subscription. |
| `nsg_id` | Required | Existing NSG ARM resource ID in the configured subscription. |
| `vm_size` | Required | Explicit SKU; preflight must confirm one GPU and applicable region/zone/market support. |
| `image_id` | Required | Managed image ID or versioned Compute Gallery image ID; `latest` and Marketplace URNs are not accepted. |
| `ssh_public_key` | Required | Public SSH key text, not a private key. |
| `capacity_type` | Required | `spot` or `on-demand`; no automatic market fallback. |
| `zone` | Empty string | Empty lets Azure select regional placement; otherwise `1`, `2`, or `3`, subject to SKU availability. |
| `public_ip` | `false` | Boolean; explicitly enables a managed Standard public IP. Private-only nodes require separately prepared outbound connectivity. |
| `os_disk_gb` | `128` | OS disk size, 32–4095 GiB; must also fit the selected image and model storage. |
| `admin_username` | `smgadmin` | Linux administrator username. |
| `operation_timeout` | `900` | Positive timeout for each Azure SDK long-running operation. A timeout leaves the reservation unresolved. |

See the [disabled Azure example](docs/examples/azure-gpu-pool.json) and
[setup/recovery guide](docs/azure.md). Credentials belong in secrets; resource IDs,
public keys and other settings can live in nonsecret configuration.

## DigitalOcean credentials

| Environment variable | Required/default | Secret? | Purpose |
| --- | --- | --- | --- |
| `DIGITALOCEAN_TOKEN` | Required for an enabled DigitalOcean pool; no default | **Yes** | Scoped API token for the configured account/team. No interactive `doctl` login is used. |

### DigitalOcean JSON settings

These are fields in `options.settings`, not separate `DO_*` environment variables.

| Field | Required/default | Meaning |
| --- | --- | --- |
| `account_uuid` | Required | `team.uuid` from `GET /v2/account` when present, otherwise `uuid`. This identifier need not be a dashed UUID. |
| `region` | Required | Exact datacenter slug, such as `mkc1`; no region-prefix selection. |
| `size` | Required | Exact supported single-NVIDIA-GPU size slug. The API catalog must confirm availability, region and one NVIDIA GPU. |
| `image_id` | Required | Positive numeric ID of a tested image; image slugs/`latest` are not accepted. |
| `vpc_uuid` | Required | Existing VPC UUID in the chosen region. |
| `firewall_id` | Required | Existing firewall UUID; must target `smgp-pool:<name_prefix>`, have no inbound rules and explicit outbound rules, and be ready. |
| `project_id` | Required | Existing project UUID to receive the created Droplet. |
| `ssh_keys` | Required | Nonempty JSON array of existing positive numeric SSH key IDs or colon-separated hexadecimal fingerprints. |
| `capacity_type` | Required | `spot` or `on-demand`; the size must end in `-spot` exactly when selecting Spot. |
| `public_networking` | `false` | Boolean; private-only by default. Prepare VPC egress separately. IPv6 remains disabled. |
| `operation_timeout` | `900` | Positive integer timeout for each action/state polling loop. |
| `poll_interval` | `5` | Integer from 1–30; delay between operation-status polls. |

See the [disabled DigitalOcean example](docs/examples/digitalocean-gpu-pool.json)
and [setup/recovery guide](docs/digitalocean.md). Replace every placeholder,
including sample numeric image/SSH-key IDs, before enabling the entry. The example
B300 Spot slug must be verified against the live account catalog; it is not a
capacity or price guarantee.

## Shared bootstrap environment

These variables remain required by the packaged Azure/DO/EC2 GPU bootstrap and
are now shared across providers. They have no default unless noted.

| Variable | Required/default | Secret? | Purpose |
| --- | --- | --- | --- |
| `GHCR_PAT` | Required for packaged scripts | **Yes** | Package-pull PAT written to the node registry configuration; username remains `agentsanchez`. |
| `K3S_URL` | Required | No | Reachable control-plane HTTPS URL, usually through Tailscale. |
| `K3S_VERSION` | Required | No | k3s release matching the control plane. |
| `K3S_KEY` | Required | **Yes** | k3s agent join token. |
| `TS_AUTHKEY` | Generated in-process by the Tailscale manager | **Yes** | Node tailnet enrollment key; manual standalone rendering must supply a test/valid key as appropriate. |
| `INSTANCE_IDENTIFIER_LABEL` | `smgp-instance-identifier` | No | Kubernetes instance-name label key. |
| `INSTANCE_PROVIDER_LABEL` | `smgp-instance-provider` | No | Kubernetes provider label key. |
| `NODE_ROLE_LABEL` | `smgp-role` | No | Kubernetes role-label key. |
| `NODE_ROLE_LABEL_VALUE` | `gpu` | No | Default GPU demand role and legacy GCE bootstrap role; Azure/DO/EC2 shared bootstrap uses the configured pool `role`. |
| `NVIDIA_CONTAINER_TOOLKIT_VERSION` | Required only by legacy `scripts/gpu-startup.sh` | No | Toolkit package version for that GCE script. Azure/DO images must already supply the tested driver/runtime. |

The main process seeds the label defaults through `k8s.py`. A standalone script
renderer must supply defaults itself or import that configuration. `__GPU_ROLE__`
is filled during bootstrap assembly from the pool's JSON `role`; it is not an
additional required environment variable. `{{INSTANCE_NAME}}` and
`{{INSTANCE_PROVIDER}}` are dynamic values supplied by the provider, not envs.
Custom `__VARIABLE__` placeholders introduce their own required environment values.

## Local emulator and test environment

These settings are for local tooling/CI, not live provisioner configuration.

| Variable | Default / set by | Purpose |
| --- | --- | --- |
| `PYTHON` | `scripts/setup_emulators.sh`: `.venv/bin/python` | Python executable used to install test dependencies. CI sets `PYTHON=python`. |
| `AWS_EC2_METADATA_DISABLED` | Test runner sets `true` | Prevents AWS metadata credential discovery in the integration process. Clients also receive explicit fake credentials. |
| `NO_PROXY` | Test runner sets `127.0.0.1,localhost,::1` | Keeps emulator traffic off inherited proxies. |

No cloud tokens, Azure credentials, AWS keys, `LOCALSTACK_AUTH_TOKEN`, or emulator
endpoint environment variables are needed for these tests. Endpoints are injected
as loopback-only test-client parameters; `DigitalOceanAPI.base_url` is a constructor
parameter, not an environment variable. The runner launches Prism with only
`PATH`, `TMPDIR`, and `SYSTEMROOT` inherited when present, excluding cloud secrets.
See [emulator setup and coverage](docs/emulator-tests.md).

## Scaleway, Nebius and OVHcloud credentials

These experimental adapters are configured only through `GPU_POOLS_JSON`; no
legacy provider-specific pool environment variables are added. All three support
User-specified purchasing settings, with multiple active nodes per pool. Keep disabled until live
acceptance. See [setup, constraints and recovery](docs/remaining-providers.md).

| Variable | Required when | Value/source |
| --- | --- | --- |
| `SCW_SECRET_KEY` | Scaleway enabled | Scoped Scaleway IAM API secret, supplied as a Secret; sent as `X-Auth-Token`. No access-key variable is consumed. |
| `NEBIUS_CREDENTIALS_FILE` | Nebius enabled | Absolute path to a read-only mounted service-account credentials JSON file supported by the Nebius SDK. The file contains private key material; do not put its contents in pool JSON. SDK renews access tokens. No CLI login fallback is configured. |
| `OS_APPLICATION_CREDENTIAL_ID` | OVHcloud enabled | OpenStack application credential ID for the configured project. |
| `OS_APPLICATION_CREDENTIAL_SECRET` | OVHcloud enabled | Matching application credential secret, supplied as a Secret. |

All use `options.ledger_path` (required, durable SQLite path),
`options.name_prefix` (optional: `smg-gpu-scw`, `smg-gpu-nebius`, `smg-gpu-ovh`),
and `options.startup_script` (optional override of shared GPU bootstrap).
`options.settings` contains the provider fields below. Credentials are never
stored in the lifecycle ledger. Existing `INSTANCE_AUDIT_LOG`, `GHCR_PAT`,
Tailscale and k3s bootstrap variables still apply. Separate pools need distinct
prefixes and roles. Only one controller may manage each pool.

### Scaleway JSON settings

| Field | Required/default | Meaning |
| --- | --- | --- |
| `project_id` | Required | Project UUID. |
| `zone` | Required | Explicit zone, e.g. `fr-par-1`. No zone failover. |
| `commercial_type` | Required | Exactly a local-SSD-compatible type from zone catalog. SBS-backed types are rejected. |
| `image_id` | Required | Pinned zone image UUID, one local-SSD root, preinstalled NVIDIA driver/runtime. |
| `security_group_id` | Required | Existing project security group UUID; provisioner does not modify rules. |
| `capacity_type` | Required | `on-demand`. |
| `boot_disk_gb` | `128` | Decimal GB local SSD; must satisfy type constraints. |
| `operation_timeout` | `900` | Seconds per operation convergence wait. |

Creates a dedicated **billable public IPv4** per VM. No private-network, floating
IP reuse, SBS, scratch formatting, or Spot implementation in this adapter.

### Nebius JSON settings

| Field | Required/default | Meaning |
| --- | --- | --- |
| `project_id` | Required | Nebius parent project ID. |
| `subnet_id` | Required | Existing subnet in that project; determines placement. |
| `image_id` | Required | Pinned compatible NVIDIA Ubuntu image ID. |
| `platform` | Required | Platform name from project catalog. |
| `preset` | Required | Preset exposing the configured GPU capacity. |
| `security_group_ids` | Required | Nonempty array of existing project group IDs. |
| `capacity_type` | Required | `on-demand` (default) or `preemptible`; native `purchase_spec` takes precedence. |
| `public_ip` | `false` | Allocate an ephemeral address on the VM interface. With false, provision working outbound connectivity on the subnet. |
| `boot_disk_gb` | `128` | Network SSD size in GiB (SDK uses GiB despite shared field naming). |
| `operation_timeout` | `900` | Seconds waiting for each asynchronous operation. |

### OVHcloud JSON settings

| Field | Required/default | Meaning |
| --- | --- | --- |
| `project_id` | Required | OpenStack project ID, must match credential scope. |
| `region` | Required | OpenStack service-catalog region. |
| `auth_url` | Required | HTTPS Keystone v3 identity endpoint from your project. |
| `flavor_id` | Required | User-selected region-specific flavor UUID. No GPU-count extra specs required. |
| `image_id` | Required | Pinned active Glance GPU image ID, preinstalled NVIDIA driver/runtime. |
| `network_id` / `subnet_id` | Required | Existing network and matching subnet; must already provide outbound routing. |
| `security_group_ids` | Required | Nonempty list of existing project security groups. |
| `key_name` | Required | Existing Nova SSH keypair name. |
| `capacity_type` | Required | `on-demand`. |
| `boot_disk_gb` | `128` | Cinder boot volume size in GiB. Must satisfy image minimum. |
| `volume_type` | Empty | Optional Cinder volume type; empty uses service default. |
| `availability_zone` | Empty | Optional Nova AZ; empty leaves placement to Nova within region. |
| `operation_timeout` | `900` | Seconds for each resource wait. |

No floating IP, network, router, IAM, or security-group rule creation. Cinder volume
project metadata must be visible to credentials for ownership validation/cleanup.


## Multi-node logical pools and ordered candidates

See [logical pool configuration and purchasing semantics](docs/logical-pools.md).
Managed pools no longer have a one-active-node maximum; `max_nodes` applies across
all candidate instances, including pending instances. Submissions remain serialized.

| Pool field | Meaning |
| --- | --- |
| `candidates` | Ordered nonempty list of `{id, provider, options}`; replaces top-level `provider`/`options`. |
| `name_prefix` | Optional logical-pool prefix, defaults to `name`; 2–21 lowercase letters/digits/hyphens. Child prefixes append stable candidate ID. |
| `placement_ledger_path` | Required with candidates: durable SQLite attempt ledger. Secrets are not stored. |

Each managed candidate retains its required `options.ledger_path`. No new credential
environment variables are needed. `GPU_EC2_CAPACITY_TYPE` now has **no implicit Spot
default**; template market settings or `EC2_CAPACITY_TYPE`/on-demand fallback apply.
DO `settings.capacity_type` is optional (derived from size). Scaleway/OVH
`settings.capacity_type` is optional. Nebius accepts `settings.purchase_spec` with
one native purchasing field; see the linked guide for schema and precedence.
