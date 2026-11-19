# Azure GPU provider: initial implementation

Azure is disabled by default. This release provides user-selected GPU VM creation,
inventory, durable launch tracking, and VM/NIC/OS-disk/optional-public-IP cleanup.
It is locally tested, not live-qualified. No Azure resources are created by setup
of the Python provider itself.

## Infrastructure prerequisites

Prepare these separately from the runtime operator:

1. A subscription and dedicated VM resource group; register `Microsoft.Compute`
   and `Microsoft.Network` in that subscription.
2. An existing VNet/subnet and NSG. Keep public inbound closed. Supply outbound
   connectivity for Tailscale, GHCR and package/bootstrap downloads: either explicit
   NAT/firewall routing or an explicitly enabled Standard public IP per VM.
   A private subnet alone does not guarantee outbound internet access.
3. GPU family/region quota and Spot quota if using Spot. Select a regional,
   single-NVIDIA-GPU SKU. The adapter checks advertised GPU count, Spot support,
   zone availability and subscription restrictions; this is not a capacity promise.
4. A generalized Linux managed image or a **versioned** Compute Gallery image,
   with compatible NVIDIA driver, `nvidia-container-runtime`, cloud-init, and Azure
   Linux Agent installed. Confirm cloud-init runs custom data. Marketplace image
   URNs, purchase plans, automatic driver installation and image building are not
   handled in this initial implementation.
5. A runtime service principal (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
   `AZURE_CLIENT_SECRET` in secrets), or supported workload federation
   (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_FEDERATED_TOKEN_FILE`). No interactive
   Azure CLI login or implicit developer credential fallback is used.
6. A persistent directory for the lifecycle SQLite database. Back it up with the
   audit log; do not place it in an ephemeral container filesystem. Keep one active
   controller per pool and keep subscription/group/prefix stable during cleanup.

Runtime permissions need VM/disk/NIC/public-IP read/write/delete in the dedicated
resource group, subnet/NSG read and join on the selected network resources, image
read, and subscription-level SKU read. Inventory includes NIC/IP/disk lists even
when public IP allocation is disabled, to detect leftovers. Shared resource group,
network, identity and role creation are setup permissions, not runtime permissions.
Review the exact scoped role against your chosen image/network before a live test.

## Configuration

Use [the disabled example](examples/azure-gpu-pool.json) as the value of
`GPU_POOLS_JSON`. Replace every placeholder before enabling it. This JSON contains
nonsecret configuration; it replaces legacy GPU pools when set. To retain GCE or
EC2 pools, include them explicitly in the list, or leave the existing deployment
unchanged while preparing the new configuration.

Required bootstrap environment remains `TS_AUTHKEY`, `GHCR_PAT`, `K3S_KEY`,
`K3S_URL`, `K3S_VERSION`, and the existing instance/provider/role label variables.
The operator's Tailscale manager supplies `TS_AUTHKEY` as before. Keep secrets in
the existing secret-delivery system. Azure custom data contains bootstrap secrets;
restrict VM/custom-data read access accordingly. They are never written to the
local lifecycle database or audit events.

Workloads select `smgp-role: gpu-azure` (or your configured role label/value) and
tolerate `nvidia.com/gpu=gpu-azure:NoSchedule`. Ensure the cluster's NVIDIA device
plugin selects this node and tolerates the taint. Untargeted GPU demand continues
to use the existing default role; creating an Azure pool does not reroute it.

The shared bootstrap writes `/etc/rancher/k3s/registries.yaml` atomically as root,
mode `0600`, before k3s starts. Azure initially uses the managed OS disk for k3s
and model storage. It does not format temporary disks or attach data disks. Set
`os_disk_gb` large enough for the image, container layers and model cache.

Spot uses `eviction_policy=Delete` and `max_price=-1` (no price-based eviction;
capacity eviction still applies). On-Demand must be explicitly configured as
`on-demand`; there is no automatic escalation to it. One optional zone can be
specified, or omitted to let Azure choose regional placement. Ordered zone/SKU
retry is not implemented yet.

## Inspecting and recovering a launch

Use the same configuration and persistent database as the controller:

```sh
python -m lifecycle_admin inspect --pool gpu-azure
```

The output lists planned native resource IDs and phase even when creation failed
midway. A lost response blocks further launch attempts. If Azure inventory later
reports a terminal VM state, reconciliation resolves that uncertainty automatically.

If no VM appears, independently check Azure's operation/activity history and all
recorded resource IDs. Stop the active controller before manual recovery. **Only
after confirming no creation operation is still pending**, mark the attempt
abandoned:

```sh
python -m lifecycle_admin confirm-abandoned --pool gpu-azure \
  --name INSTANCE_NAME --confirm-no-pending-cloud-operation
```

This changes only local recovery metadata. It does not delete cloud resources.
The normal controller deletion path can subsequently clean the recorded
auxiliaries, after its provisioning timeout. Cleanup failures retain the record
and retry; do not delete the database to unblock launches. If metadata was lost,
restore its backup. Unknown tagged resources block the pool rather than being
automatically adopted.

## Remaining acceptance work

* Choose and verify the real subscription, network, SKU, image and runtime role.
* Run a controlled one-VM test: k3s join, one GPU advertised, authenticated GHCR
  workload, and complete cleanup confirmed against every recorded resource ID.
* Exercise real Spot eviction and partial-operation recovery.
* Add Scheduled Events polling and bounded draining as a separate interruption
  component. This version handles disappearance, but does not proactively drain
  on an Azure eviction notice.
* Add ordered compatible placements, durable GCE/EC2 migration, then GPU provider
  failover. Confidential GPU attestation remains a separate project.

Argo operations are user-controlled; this implementation does not sync or modify
Argo applications.

References: [Azure VM creation](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/create-or-update),
[attached-resource deletion](https://learn.microsoft.com/en-us/azure/virtual-machines/delete),
[SKU availability](https://learn.microsoft.com/en-us/azure/azure-resource-manager/resource-manager-sku-not-available-errors),
[Scheduled Events](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/scheduled-events).

Updated pool cardinality, placement ordering and purchasing behavior: [logical pools](logical-pools.md). This supersedes initial single-node and GPU-count restrictions above.
