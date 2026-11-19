# Logical pools and explicit placement preferences

A pool is a provisioner abstraction, independent of native cloud pooling. Its
`max_nodes` bounds the aggregate instances across its candidates. Each managed
instance has its own durable VM/disk/network record. Multiple ready instances can
coexist; only one unresolved submission is allowed per candidate scope. Cloud
submissions are serialized, not launched in parallel. Pending instances count
toward desired capacity. The current GPU demand policy remains pod-count based,
not GPU bin packing; users choose appropriate workload routing and instance sizes.

## Configuration

Existing `provider` + `options` pool entries continue to work. Alternatively use
`candidates`, an ordered nonempty list, with one shared role and capacity limit:

```json
[{
  "enabled": false,
  "name": "gpu-main",
  "name_prefix": "gpu-main",
  "role": "gpu-main",
  "max_nodes": 4,
  "placement_ledger_path": "/var/lib/smg-cloud-provisioner/placements.sqlite",
  "candidates": [
    {"id": "west", "provider": "ec2", "options": {
      "region": "us-west-2", "template": "lt-REPLACE-WEST", "version": "1",
      "subnet_ids": ["subnet-REPLACE-WEST"]
    }},
    {"id": "east", "provider": "ec2", "options": {
      "region": "us-east-1", "template": "lt-REPLACE-EAST", "version": "1",
      "subnet_ids": ["subnet-REPLACE-EAST"]
    }}
  ]
}]
```

Candidates may use the same or different registered providers, templates, regions,
zones and instance specs. Managed candidates also require their normal
`options.ledger_path` and `options.settings`. No catalog chooses another SKU.
Candidate IDs are stable 1–10 character lowercase alphanumeric identifiers starting
with a letter. Prefixes derive from pool prefix plus candidate ID. Keep IDs and
prefixes unchanged while resources exist. Reordering candidates preserves identity;
removing candidates or changing their project/region while resources exist is not
supported—clean up first. `name_prefix` is 2–21 lowercase letters/digits/hyphens.
Do not supply per-candidate `name_prefix`. Run one controller for a logical pool.

A failure advances the preference cursor for the next reconciliation; success
resets it to the first candidate. Restart also starts preference at the first
candidate. Every candidate's inventory must succeed before another launch.
Preflight failures with no managed submission and explicitly confirmed EC2
rejections permit fallback. Unknown outcomes, partial managed launches, and failed
cleanup block new allocation until resolved. This favors avoiding duplicate paid
resources over immediate fallback. The preference cursor is not a regional race.

The placement ledger reserves an attempt before invoking a candidate. Successful
responses remain tracked until inventory observes the returned instance name. A lost reply
can be resolved by a single new instance in that candidate's inventory; no instance
or multiple matches remain blocked. For manual recovery, use `lifecycle_admin.py`
with the logical pool name. `confirm-abandoned` requires independent verification
that no request can allocate late. Child managed-provider reservations may also
need recovery using a temporary direct pool configuration with the same scope and
ledger; confirming only the parent does not clear child uncertainty.

## User-supplied purchasing and instance specifications

- **GCE:** template scheduling/market settings are unchanged.
- **EC2:** template `InstanceMarketOptions` is honored through `RunInstances`.
  An explicit `capacity_type` must agree with the template. Template-free market
  settings continue through instant Fleet with explicit `capacity_type`, environment
  fallback, or AWS's on-demand default. GPU EC2 configuration no longer silently
  defaults to Spot. Template-market launches use the first eligible configured
  subnet; use candidates for an explicit subnet preference/fallback sequence.
  Current lifecycle supports one-time terminating Spot, not persistent Spot requests
  or stop/hibernate. Fixed placement/network-interface template restrictions remain.
- **Azure:** the supplied `capacity_type` selects Spot/Regular; SKU GPU counts are
  no longer restricted to one. Existing image, zone and subscription checks remain.
- **DigitalOcean:** size slug supplies the purchasing selection. `capacity_type`
  is optional and, if provided, must agree. GPU model/count allowlists are removed.
- **Nebius:** `settings.purchase_spec` accepts exactly one native field:
  `on_demand`, `preemptible`, `follows_spot_price`, or `spot_pricing_policy`, with
  its SDK-shaped object. Example: `{"preemptible":{"on_preemption":1}}` (STOP),
  or `{"spot_pricing_policy":{"id":"USER-POLICY-ID"}}`. It takes precedence over
  the compatibility `capacity_type` field (`on-demand` default, or `preemptible`).
  The adapter validates SDK field construction before allocating a disk. Catalog
  validation confirms the supplied platform/preset exists, not a one-GPU policy.
- **Scaleway/OVHcloud:** the supplied type/flavor defines the offering. No GPU-count
  allowlist or OVH extra-spec GPU-count requirement remains. `capacity_type` is
  optional; the legacy value `on-demand` is accepted. Arbitrary `spot` labels cannot
  create a purchasing feature without a supported native API field; these adapters
  do not currently implement such a field. Scaleway SBS lifecycle remains separate.

The default bootstrap still expects an NVIDIA-compatible image/runtime. Removing
GPU catalog restrictions does not add AMD runtime support. Templates/specs cannot
override ownership tags, resource tracking or cleanup guards.

## Migration and verification

Opening the SQLite store automatically replaces the old one-active-instance index
with a one-unresolved-submission index. Existing records remain intact. Stop old
controllers before upgrading and retain a backup; older code must not reopen a
multi-node database because it expects a single active record.

Unit coverage includes multi-node restart/deletion, atomic reservations, pending
capacity accounting, ordered selection, stable candidate IDs, lost-response recovery,
SDK purchasing payloads and EC2 template-market requests. Live GPU boot and cloud
acceptance remain separate. No Argo operation is required by these code changes.

References: [AWS RunInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html),
[Nebius API definitions](https://github.com/nebius/api).
