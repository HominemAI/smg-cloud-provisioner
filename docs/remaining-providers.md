# Scaleway, Nebius and OVHcloud: initial adapters

Implementation is available through `GPU_POOLS_JSON`, disabled unless explicitly
enabled. These are **experimental, locally tested adapters, not live-qualified
providers**. No cloud resources were created during implementation. They share
`ManagedProvider`, the durable SQLite ledger, audit events, scheduler and GPU
bootstrap. Each adapter owns only its cloud-specific requests and cleanup order.

See [all settings and credentials](../ENV.md#scaleway-nebius-and-ovhcloud-credentials)
and [disabled examples](examples/remaining-gpu-pools.json). Do not enable the
placeholder examples. `GPU_POOLS_JSON` replaces existing GPU pool configuration;
merge entries you want to retain. Emergency pools remain separate.

## Implemented boundaries

| Provider | Launch | Tracked resources | Current limits |
| --- | --- | --- | --- |
| Scaleway | Reserve IPv4, create stopped VM/root, upload cloud-init, power on | VM, local root volume, IPv4 | Explicit zone, local SSD only |
| Nebius | Create network SSD from image, wait, create VM | Disk and instance IDs plus create operation IDs; interfaces/ephemeral address belong to VM | User-selected preset/purchasing, project/subnet placement |
| OVHcloud | Create boot volume, Neutron port, Nova server | Volume, port and server IDs | On-demand; catalog/image/network qualification below |

Every resource ID is checkpointed before the next dependent operation. Lost
responses leave the slot reserved. Inventory recovers IDs by pool ownership;
duplicate matches or changed identities block cleanup. Auxiliary resources left
after VM disappearance remain visible until deletion succeeds. Foreign disks,
ports or changed ownership block deletion. Networks, groups, images and keys are
operator-owned and never deleted. Scaleway inline root volumes lack creation-time
tags: recovery additionally requires exact generated root name and project.

Explicit ordered candidate fallback is available; see [logical pools](logical-pools.md).
No Spot interruption event draining, scratch formatting, regional capacity race
or GPU bin packing is included. Existing GCE/EC2 lifecycle migration is separate work.

## Prepare and qualify a provider

1. Create the project, scoped service identity, network/subnet with DNS and
   outbound HTTPS, and appropriate security groups. Existing Tailscale connectivity
   allows no inbound public rules; allow the outbound traffic required by your
   tailnet policy and bootstrap. Test this routing independently.
2. Build or select a pinned Ubuntu image with a compatible NVIDIA driver,
   `nvidia-smi`, NVIDIA container runtime, cloud-init, and systemd. The shared hook
   verifies these tools but does not install GPU drivers. Model data and k3s use
   the boot disk; teardown destroys that data.
3. Choose the desired offering and obtain quota. For Scaleway, check catalog
   local-SSD support and image root type; SBS GPU offerings currently need another
   storage adapter. For Nebius, match the project platform/preset catalog.
4. For OVHcloud, supply the exact flavor ID. No GPU-count metadata is required.
   Confirm the selected image boots from Cinder and volume project IDs are visible
   under the application credential. These still need live qualification.
5. Mount credentials as Secrets, persist `ledger_path`, configure existing GHCR,
   Tailscale and k3s variables, and run one controller. Set `INSTANCE_AUDIT_LOG`
   to a persistent JSONL file. Never commit credential files or rendered userdata.
6. Review the pool JSON before enabling. With authorization for a billable test,
   launch one node; record all native IDs from the ledger/audit. Verify GHCR pull,
   Tailscale, k3s registration/taints and a GPU workload. Then delete through the
   provider and verify all owned resources disappeared and the ledger is deleted.
   Test failed bootstrap, controller restart and lost API response before production.

## Recovery

Use [lifecycle_admin.py](../lifecycle_admin.py) to inspect the durable metadata.
A partial uncertain launch is not automatically abandoned after a timeout. Confirm
in the provider control plane that no operation can create a late resource before
using `confirm-abandoned --confirm-no-pending-cloud-operation`; ordinary cleanup
can then remove tracked auxiliaries. Never erase the ledger to force another launch.
Nebius operation IDs aid that check. Raw SDK errors and bootstrap secrets are not
written to the ledger. No Argo operation is part of this procedure.

## Local coverage and next validation

`tests/test_remaining_providers.py` exercises official Nebius message construction,
OpenStack SDK field mapping, resource checkpoint order, failed operations, pagination,
ambiguous recovery and refusal to delete foreign attachments. Scaleway transport
rejects redirects and suppresses API error bodies. These are adapter unit tests with
fake cloud responses, **not provider emulators or live acceptance tests**. Existing
Moto/Prism/ARM integration coverage is unchanged; these new adapters still need
HTTP/gRPC integration fixtures and an authorized live acceptance run.

Runpod feasibility is documented [separately](runpod-feasibility.md); it is not a
registered node provider.

## Primary API references

- [Scaleway Instance API](https://www.scaleway.com/en/developers/api/instance/v1/)
- [Nebius Python SDK](https://github.com/nebius/pysdk) and [API definitions](https://github.com/nebius/api)
- [OpenStack SDK compute](https://docs.openstack.org/openstacksdk/latest/user/proxies/compute.html), [network](https://docs.openstack.org/openstacksdk/latest/user/proxies/network.html), [block storage](https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage_v3.html)
- [OVHcloud GPU deployment](https://docs.ovhcloud.com/en/guides/public-cloud/compute/deploy-a-gpu-instance) and [native flavor catalog](https://developers.eu.ovhcloud.com/reference/public-cloud/compute/instances/)

Updated pool cardinality, placement ordering and purchasing behavior: [logical pools](logical-pools.md). This supersedes initial single-node and GPU-count restrictions above.
