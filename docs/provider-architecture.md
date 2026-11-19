# Provider architecture

The scaler is independent of cloud SDKs. The composition root selects providers;
adding a provider requires an adapter and a registry entry, not another scaler branch.

| Responsibility | Boundary | Current implementation |
| --- | --- | --- |
| Scaling decisions | `reconcile(pool, now, instances, nodes)` | `GpuPolicy`, `EmergencyPolicy` |
| Cluster operations | `Scheduler` protocol | `KubernetesScheduler` |
| Existing scaler/provider contract | `CloudProvider` | GCE, EC2, managed Azure/DigitalOcean/Scaleway/Nebius/OVHcloud |
| Provider construction and capabilities | `ProviderFactory` | Lazy registrations, no SDK construction for disabled pools |
| Cloud operations and normalized state | `ResourceDriver` protocol | Azure, DigitalOcean, Scaleway, Nebius, OVHcloud drivers |
| Launch reservation and recovery | `ManagedProvider` | Reusable asynchronous lifecycle orchestration |
| Persistent metadata | `LifecycleRepository` protocol | Transactional SQLite `LifecycleStore` |
| Common GPU bootstrap | `gpu_bootstrap(hook, role)` | Shared registry/Tailscale/k3s script with cloud-specific or preinstalled-driver preparation hooks |

These boundaries apply SOLID through small responsibilities, registration-based
extension, a common provider contract, narrow scheduler/driver/storage interfaces,
and dependency injection for SDK clients and persistence. Capabilities describe
implemented behavior; they do not imply available capacity or equivalent GPUs.

`GPU_POOLS_JSON`, when present, replaces the legacy GPU pool settings. Every entry
is disabled unless `enabled` is the boolean `true`. Omit it to preserve existing
GCE/EC2 environment behavior. Emergency configuration remains independent.
Roles must differ between pools so one pending workload does not start several
providers. Within a logical pool, ordered candidates provide explicit placement fallback;
separate pool roles still require explicit workload routing.

## Lifecycle invariants

* Reserve one pool slot and deterministic resource IDs durably before cloud writes.
* Persist metadata only: IDs, scope, phase, timestamps, exception type. Never store
  rendered bootstrap, PATs, client secrets, auth keys, or raw SDK errors.
* Inventory errors are errors, never an empty list. Unknown owned resources stop
  reconciliation rather than being silently adopted or deleted.
* Uncertain submission remains reserved across restarts. A terminal VM state can
  resolve it; elapsed time or temporary absence cannot.
* Listing never writes cloud resources, including in dry-run mode. Evicted VMs
  remain represented until the ordinary delete path removes their auxiliaries.
* Cleanup is retryable and only uses the matching durable resource record.
  Shared networks, groups, IAM identities, and images are never deleted.

The managed adapters currently support multiple active instances per pool. The lifecycle
database must be on persistent local storage and backed up; SQLite reservations
are atomic for controllers sharing the same database. This is not a distributed
lease across independent databases. Run one active controller per owned pool.

GCE and EC2 retain their existing native implementations, audit logs, and startup
compatibility. Their old in-memory launch reservations have **not** been migrated
to the durable managed lifecycle yet. The isolated adapters allow that migration
without changing scaling policy; do not assume the new restart guarantees apply
to them. The standalone EC2 startup script remains a compatibility artifact;
new default EC2 pools assemble the shared bootstrap and EC2 storage hook.

## Additional providers

| Provider | Driver work | Reused components |
| --- | --- | --- |
| DigitalOcean (implemented, pending live validation) | Droplet/action API, deterministic naming/tags, paginated inventory, Spot size validation, deletion confirmation | Managed lifecycle, store, scheduler, GPU policy, bootstrap core |
| Scaleway (initial adapter) | Instance and volume APIs, explicit IP ownership, image and GPU offering validation | Same components; provider-specific storage preparation |
| Nebius (initial adapter) | Asynchronous operation tracking, VM/disk/network ownership, GPU platform validation | Same components; provider-specific bootstrap preparation |
| OVHcloud (initial adapter) | OpenStack server/volume/port lifecycle and scoped inventory | Same components; provider-specific deletion dependencies |
| Runpod | First prove host privileges and networking are suitable for a k3s node | Adapter only if feasibility succeeds |

Drivers normalize provider-specific states into `READY`, `FAILED`, `PENDING`, or
`ORPHANED`. They validate images, size, network placement and user-data limits;
they decide resource creation/deletion order. They must not decide desired node
counts or silently change market type. All iterators must exhaust pagination and
propagate failures, including failures after the first page.

Before adding ordered GPU failover, migrate all participants to durable lifecycle
tracking and define explicit compatibility requirements (GPU memory/runtime) and
cooldowns. An uncertain submission must block fallback. Future interruption
handling should be a separate event-source interface; it should not be mixed into
cloud construction, scaling demand, or image setup.

## Validation

Run `.venv/bin/python -m unittest discover -s tests -q`. Tests cover the original
GCE/EC2 behavior, generic driver recovery, scheduler injection, registry extension,
Azure ownership and cleanup, SDK serialization, and rendered bootstrap syntax.
Cloud deployment and real GPU workload validation are separate acceptance steps.

Server-assigned IDs are supported through the driver’s `resources_changed` callback
and `discover_resources` recovery method. The repository appends new IDs atomically
and rejects changes to established identities. Azure uses predetermined ARM IDs;
DigitalOcean persists Droplet/action IDs as soon as they arrive and recovers lost
responses through complete owned-resource inventory. Cloud requests and HTTP
transport stay in separate modules (`digitalocean_provider.py`, `digitalocean_api.py`).

See [additional adapter limitations and setup](remaining-providers.md) and [Runpod feasibility](runpod-feasibility.md).

Updated pool cardinality, placement ordering and purchasing behavior: [logical pools](logical-pools.md). This supersedes initial single-node and GPU-count restrictions above.
