# SMG Cloud Provisioner

A Kubernetes node provisioner for hybrid K3s, GCE and EC2 environments. Independent GPU
pools scale GCE or EC2 instances with GPU pod count. The optional emergency CPU pool
launches when all non-provisioner workers are unavailable, and drains after
workers have recovered continuously for the configured delay.

## Configuration and execution

See [ENV.md](ENV.md). Pools have independent state and limits. Disabled
providers need no credentials. Kubernetes initialization is explicit; importing
modules no longer connects to the cluster or starts watchers.

```sh
python -m unittest discover -s tests -v
docker build -t smg-cloud-provisioner:emergency-test .
```

Deploy a single replica with `Recreate` strategy: inventory reconciliation avoids
restart duplicates, but there is no distributed leader election. Retain the
existing control-plane placement, toleration and `dnsPolicy: Default`. RBAC needs
node list/patch/delete, pod list, and `create` on the core `pods/eviction` subresource.

Start with `DRY_RUN=true`. This reads live cloud/Kubernetes inventories and logs
decisions without mutations. The
standard script registers the taint `smgp-emergency=true:NoSchedule`; opt workloads
in with this toleration:

```yaml
tolerations:
  - key: smgp-emergency
    operator: Equal
    value: "true"
    effect: NoSchedule
```

## Creating the GCE CPU template

Create a regional CPU template with a standard Ubuntu 24.04 image, sufficient
RAM for your workloads, no GPU accelerators, outbound internet access, and a
boot disk deleted with the VM. For example, e2-standard-8 provides 32 GiB RAM.
Set `EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME` and `EMERGENCY_PROVIDERS=gce`.
Supply [scripts/emergency-startup.sh](scripts/emergency-startup.sh) through
`EMERGENCY_STARTUP_SCRIPT`; credentials must come from Kubernetes Secrets.

## EC2 capacity across Availability Zones

EC2 launches use a one-instance **instant Fleet** in one region. Configure
`AWS_SUBNET_IDS` with subnets from multiple AZs and set `EC2_CAPACITY_TYPE=spot`
for price-capacity-optimized Spot allocation. On-demand remains the default.
The provider resolves the template version, checks subnet VPC membership and
instance-type offerings, then lets AWS allocate among eligible subnets.

Remove fixed AZ/network-interface and market-option settings from the launch
template; provide its security-group IDs instead. See [ENV.md](ENV.md) for the
configuration and additional IAM permissions. Existing templates that pin these
settings fail validation before any Fleet request is submitted.

A separate EC2 GPU pool is available through `GPU_EC2_ENABLED=true`; it defaults
to Spot and requires workloads to select its role, `smgp-role: gpu-aws`.
The EC2 emergency pool must use a CPU template; do not point it at an H100 template
as a substitute for a separate GPU pool. Fleet integration has local SDK tests;
a live AWS launch requires the regional template, subnets, credentials and quotas.

## Failover, recovery and tracking

`EMERGENCY_PROVIDERS=ec2,gce` sets the default order; `gce,ec2` reverses it. A
failed launch or join timeout advances the next attempt. Before retrying, the
controller reconciles inventories for every configured provider and waits out any
outstanding launch reservation. Inventory errors block new launches, because an
unreachable provider may already have an instance. Existing instances on either
provider prevent another launch. Recovery to an empty pool resets priority order.

Recovery cordons and evicts workload pods, waits for them to disappear, deletes
the Kubernetes node, then terminates the instance. DaemonSet and mirror pods are
excluded. PodDisruptionBudgets can delay teardown indefinitely. Renewed worker
loss cancels a partial drain and uncordons the emergency node.

The instance audit log records names, provider, project/zone or region, timestamps,
requests and completion events. EC2 termination submission is not confirmation
that an instance has finished terminating. Preserve the log and verify inventory
after a drill. Set `INSTANCE_AUDIT_LOG` to a persistent mounted path so it
survives pod replacement. Keep credentials, infrastructure manifests, live
inventories, and operational rollback records in your private infrastructure
repository rather than this source repository.

## AWS GPU bootstrap

The packaged AWS script expects a compatible Ubuntu GPU base AMI with NVIDIA
container runtime installed. It uses instance-store NVMe for K3s data and exposes
`/mnt/smg-models` for explicitly mounted model caches. It accepts the DLAMI NVMe
mount or one unpartitioned instance-store disk and refuses unexpected layouts.
NVMe data is disposable; node-local PVCs do not become portable.
GHCR credentials are written with mode 0600 before K3s starts.

Release version tags through the repository's Docker Image Publish workflow.
It authenticates to GHCR using its own GITHUB_TOKEN with packages:write; the
cluster's GHCR pull credential does not need publishing permissions.

## Limits

- Available workers must be Ready, uncordoned, non-control-plane and not labelled
  `smg-cloud-provisioner=true`. Provisioner GPU nodes do not mask an outage.
- GPU scaling compares pod count to node count, without GPU bin packing.
- Outage/recovery and first-seen provisioning timers are in memory; restarts delay
  decisions. One active controller is required, including during rollouts.
- Unready GPU cleanup retains the existing node-age timeout behavior.
- Node-local persistent volumes do not move. An emergency node can acquire local
  data through a newly bound claim; drain logs affected pod claims, but teardown
  still destroys the local data. Distributed storage is separate work.
- CPU capacity cannot run GPU workloads. Machine size and workload tolerations
  determine which services can recover.

## AI Disclosure
AI was used to assist in the creation of this code. The model did not design the architecture
and is responsible for less than 5% of the code. All code was reviewed and tested by me.

- Assisted in writing less than 5% of the code including some helper functions.
- Generating some of the docs and collecting env vars/cataloging them.
- Reviewing code for potential bugs and improvements. Improvements were implemented by human.
- A rubber duck to go over my ideas with and get feed back on my approach and understanding.
- Generated most things above this Disclosure, I like writing docs, but I mean genAI is badass.
