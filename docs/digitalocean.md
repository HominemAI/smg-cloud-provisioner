# DigitalOcean GPU provider

The `digitalocean` provider uses the same managed lifecycle as Azure. It is
disabled by default and locally tested, not live-qualified. It supports one
single-NVIDIA-GPU Droplet per pool, an explicitly selected datacenter, and either
an explicit Spot size slug or an explicit On-Demand size slug. There is no price
class fallback, region racing, or automatic switch between NVIDIA and AMD.

## Prepare the account

1. Enable GPU access and sufficient quota in the intended DigitalOcean account/team.
2. Create a VPC in the chosen datacenter and a project for these nodes.
3. Create a Cloud Firewall targeting the tag `smgp-pool:smg-gpu-do` (substitute
   your pool prefix). Use no inbound rules and explicit outbound access for
   Tailscale, DNS, HTTPS/HTTP bootstrap and image pulls. The adapter verifies the
   tag, empty inbound rules, nonempty outbound rules, and successful firewall
   status. It does not modify shared firewall or VPC configuration.
4. Upload an SSH public key. Use its existing numeric ID or fingerprint.
5. Select a tested NVIDIA image with cloud-init, compatible NVIDIA drivers and
   `nvidia-container-runtime`. Resolve its numeric image ID and pin that ID.
   Validate driver/CUDA compatibility with B300 if using that GPU generation.
6. Create a scoped API token and deliver it as the secret environment variable
   `DIGITALOCEAN_TOKEN`. Runtime needs account read, Droplet read/create/delete,
   image/SSH-key/VPC/firewall/project reads, tag creation, and project resource
   assignment permissions. Consult the endpoint scope requirements when creating
   the token; the adapter does not create tokens or change permissions.

Read `GET /v2/account` and use `team.uuid` when a team is present; otherwise use
`uuid` for `account_uuid`. This value is not necessarily a conventional dashed
UUID. Each inventory and mutation checks that the token still belongs to the
configured active account/team. Do not store the token in pool JSON or Git.

## Configure the pool

Start with [the disabled example](examples/digitalocean-gpu-pool.json). Use the
list as `GPU_POOLS_JSON`, or add its entry to your existing list of GPU pools.
That variable replaces legacy GPU pool construction, so preserve other desired
pools in the list. The role `gpu-do` isolates demand; workloads need the matching
role selector and `nvidia.com/gpu=gpu-do:NoSchedule` toleration.

The sample B300 Spot slug is a **candidate**, not a promise of current account
access or capacity. Before enabling, verify the exact slug with `GET /v2/sizes`.
Preflight requires its `available` flag, selected datacenter in `regions`, and
the exact user-supplied size slug (without GPU model/count allowlists). The image must also be available in
that datacenter. Unsupported or missing catalog entries fail before creation.
Spot requires a `-spot` size suffix and `capacity_type: spot`. There is no separate
invented Spot request flag. Published pricing is not hardcoded; preflight logs
the catalog's hourly quote, which is not a price lock or spending cap.

`public_networking` defaults to false, with IPv6 disabled. Private-only Droplets
require separately prepared VPC egress (for example NAT); Tailscale still needs
outbound connectivity. If choosing `public_networking: true`, the existing
pool-tagged firewall is still required. No reserved IP is allocated.

Mount the lifecycle database directory on persistent storage, alongside the
existing instance audit log. Run one active controller for each owned pool.
Keep `GHCR_PAT`, Tailscale and k3s credentials in the existing secret delivery
system. Common bootstrap installs GHCR credentials mode `0600` before k3s starts.
The DigitalOcean hook uses the boot disk for k3s and models, verifies NVIDIA tools,
and never formats or mounts unidentified scratch disks.

## Tracking, recovery and cleanup

Before submission the journal stores the account, region, name and ownership
tags. Immediately after the API responds it appends the Droplet ID/URN, followed
by action IDs, before project assignment or polling. IDs cannot overwrite an
existing resource identity. No user data, token or raw API error body is persisted.

A create request is submitted once. Timeouts, 429/5xx responses and rejected
requests do not trigger another POST or an On-Demand fallback. An uncertain
reservation blocks the pool until resolved. Full paginated inventory recovers a
lost response by exact name and tags; duplicate names or missing ownership fail
closed. A discovered Droplet ID is persisted before the reservation becomes ready.
If the create response was lost before project assignment, the Droplet can remain
in the account's default project; inspect and assign it to the intended project
manually. Recovery and cleanup use account ownership, not project membership.

Inspect records using the configured environment:

```sh
python -m lifecycle_admin inspect --pool gpu-do
```

If no Droplet appears, confirm in DigitalOcean that no creation action remains
pending before using the explicit `confirm-abandoned` recovery command documented
in [Azure recovery](azure.md#inspecting-and-recovering-a-launch). Absence alone is
not sufficient proof. Never delete the journal to permit another launch.

Cleanup deletes only the recorded Droplet ID with matching ownership and waits
for an individual GET to return 404. Its boot/scratch disks and ordinary network
interfaces are part of that Droplet. This adapter does not create separate
volumes, backups, snapshots or reserved IPs. It refuses deletion if the Droplet
reports attached volumes, backups or snapshots added outside the adapter; review
those resources explicitly. It never uses bulk tag deletion or the dangerous
destroy-all-associated-resources endpoint. Shared project/VPC/firewall/SSH keys
remain intact. Empty ownership tags are retained for audit and are not billable
compute resources.

## Live acceptance still required

* Confirm exact GPU Spot size, pinned image, quota, token scopes and egress.
* Create one Droplet, verify the project/firewall, k3s join, one advertised GPU,
  and an authenticated GHCR GPU workload.
* Confirm full resource-ID tracking and deletion, plus lost-response recovery.
* Validate real Spot reclamation. This implementation reconciles disappearance;
  proactive interruption notifications/draining and scratch-disk tuning are not
  implemented. Ordered cross-provider GPU failover remains separate work.

No live creation, infrastructure setup, release, or Argo action is part of adding
this adapter.

References: [GPU creation](https://docs.digitalocean.com/products/droplets/how-to/gpu/create/),
[Droplets API](https://docs.digitalocean.com/reference/api/reference/droplets/),
[size schema](https://github.com/digitalocean/openapi/blob/main/specification/resources/sizes/models/size.yml),
[create schema](https://github.com/digitalocean/openapi/blob/main/specification/resources/droplets/models/droplet_create.yml),
[Spot capacity](https://docs.digitalocean.com/products/droplets/details/spot-gpu-capacity/).

Updated pool cardinality, placement ordering and purchasing behavior: [logical pools](logical-pools.md). This supersedes initial single-node and GPU-count restrictions above.
