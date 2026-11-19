# Runpod feasibility

Assessed 2026-11-17. Research only; no resources created or credentials configured.

## Decision

**Defer Runpod as a Kubernetes node provider.** Its documented Pods product is a managed container environment, and our current bootstrap requires control of a Linux node. A Runpod Pod is not a Kubernetes Pod in our cluster. Consider a separate workload/inference backend if running a model container directly meets the requirement.

This is a compatibility decision, not a claim that every possible nested Kubernetes configuration is impossible. We have not validated a supported privileged-container or VM offering that satisfies our node requirements. Do not register a runnable Runpod provider until those requirements are demonstrated.

## Bootstrap compatibility

Our [shared GPU startup script](../scripts/gpu-common-startup.sh) installs packages, starts `tailscaled` through systemd, obtains a kernel-interface Tailscale IP, writes GHCR authentication under `/etc/rancher/k3s`, waits for NVIDIA devices, and installs a k3s agent using the NVIDIA container runtime. It binds k3s to that Tailscale address. The [provider architecture](provider-architecture.md) assumes that successful provisioning ultimately produces a schedulable cluster node.

| Requirement | Evidence and assessment |
| --- | --- |
| Linux host control | Runpod deploys Docker images and explicitly says users cannot start their own Docker instance. This is a verified restriction; it does not, by itself, prove that every containerd configuration is impossible. It does mean we cannot treat the product as an ordinary VM. [Pods overview](https://docs.runpod.io/pods/overview) |
| Root, systemd, cgroups, mounts | A root shell inside a container does not establish host privilege. We have not verified a supported systemd PID 1, writable/delegated cgroups, mount propagation, required capabilities, or host device delegation. These are unresolved requirements, not documented blanket prohibitions. |
| Tailscale kernel networking | Normal Linux Tailscale uses `/dev/net/tun`. Userspace mode provides a SOCKS5/HTTP proxy instead. That is not a replacement for the interface address our bootstrap uses for k3s binding and node networking. Runpod TUN and `NET_ADMIN` availability remain unverified. [Tailscale userspace networking](https://tailscale.com/docs/concepts/userspace-networking) |
| Cross-provider networking | Runpod documents TCP/HTTP port exposure and no UDP connections. Its global network connects Pods within one Runpod account, rather than our existing mixed-provider cluster. Neither establishes our required cluster connectivity. A TCP relay might enable Tailscale connectivity, but does not resolve the missing kernel/runtime capabilities. [Pods overview](https://docs.runpod.io/pods/overview), [global networking](https://docs.runpod.io/pods/networking) |
| GPU scheduling | Access to a GPU inside the outer Runpod container does not prove that nested containerd can delegate it to Kubernetes workloads. Require an actual NVIDIA device-plugin test and `nvidia.com/gpu` allocation on the joined node before claiming support. |

## Storage and interruption

Runpod distinguishes ephemeral container storage, a volume disk tied to the Pod lease, and independently retained network/global volumes. Container disk contents disappear on stop/restart; volume disk data lasts until termination. Our k3s data directory and model cache cannot casually remain on the container filesystem. Independent storage must be explicitly tracked and retained or deleted according to ownership. [Storage options](https://docs.runpod.io/pods/storage/types)

The current storage guide and v2 API disagree about how many network volumes can attach and whether mounts can change after creation. Do not encode assumptions from the older single-volume description; validate the current API contract before implementing storage management. [Storage options](https://docs.runpod.io/pods/storage/types), [create API](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)

**Spot support and interruption notice are not verified for the current API.** The reviewed pricing page documents on-demand allocation and deprecated savings plans, but does not specify a current Spot contract. Do not reuse historical claims about a five-second notice, bid fields, or automatic restart. Obtain a current supported creation schema and interruption lifecycle before offering a Spot configuration. Independently of Spot, the documentation describes low-balance shutdown/termination behavior; workloads must tolerate abrupt loss. [Pricing](https://docs.runpod.io/pods/pricing)

## API and lifecycle requirements for any future adapter

The current documentation identifies `https://api.runpod.io/v2` as the management API and says REST v1 is no longer available. Do not build a new adapter around `rest.runpod.io/v1`. [API overview](https://docs.runpod.io/api-reference-v2/overview)

The create schema supports image/template selection, GPU constraints, data-center placement, environment variables, and a registry credential ID. Responses expose resource IDs and caller-visible tags. The reviewed create documentation does not establish a server-side idempotency-key guarantee or atomic ownership-tag creation. Those semantics require validation; a missing tags field must not be interpreted as proof that a resource is unowned. [Create a Pod](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)

Proposed requirements, following our existing managed-provider design:

1. Persist an operation ID and intended ownership identity before submitting creation. Record the returned Pod ID immediately. Never assume names are globally unique.
2. Reconcile ambiguous create timeouts against complete inventory before retrying. If uniqueness cannot be established, retain the unknown operation and block additional allocations for that pool.
3. Treat listing, authentication, permission, and schema failures as errors, never as an empty inventory. Confirm the account/team scope and tagging permissions before relying on inventory for cleanup.
4. Initially disable queued creation. If later enabled, persist both queue ID and resulting Pod ID and cancel outstanding queue requests during cleanup. Queued requests can create resources later; their lifecycle is separate from an active Pod. [Queued Pods API](https://docs.runpod.io/api-reference-v2/pods/list-queued-pods)
5. Use explicit termination for compute cleanup, then verify disappearance. The API exposes `DELETE /v2/pods/{id}`; stopping is not an adequate substitute for deleting an allocation. [Terminate API](https://docs.runpod.io/api-reference-v2/pods/terminate-a-pod)
6. Maintain a cleanup ledger containing Pod/queue IDs, data center, operation ID, ownership marker, storage IDs, registry credential IDs, and final cleanup status. Only delete storage and registry credentials that this operation owns; preserve user-supplied shared resources.
7. Supply private-image credentials through a supported registry credential reference, with secrets redacted from logs and lifecycle records. Our existing host-side `registries.yaml` configures inner k3s pulls; it cannot authenticate Runpod's outer image pull.

## Reconsideration gate

Before implementing a node adapter, obtain a supported configuration for systemd or an alternative supervisor, cgroup delegation, privileged mounts, TUN/required capabilities, and nested NVIDIA container runtime operation. Then run an explicitly authorized disposable test: join one node, verify bidirectional cluster traffic, advertise and schedule one GPU workload, pull a private GHCR image, restart, simulate loss, and prove reconciliation and complete cleanup. Record every allocation and auxiliary resource.

If those capabilities are unavailable, keep Runpod behind a separate workload execution interface. That interface would submit container jobs or expose inference endpoints, and should not report capacity as Kubernetes nodes or participate in the emergency-node pool. No Runpod environment variables are introduced by this assessment.
