"""Scheduler boundary: scaling policies do not construct Kubernetes clients."""
from typing import Protocol
import k8s


class Scheduler(Protocol):
    def pool_nodes(self, role): ...
    def available_workers(self) -> int: ...
    def gpu_demand(self, role) -> int: ...
    def ready(self, node) -> bool: ...
    def drain(self, node) -> bool: ...
    def deregister(self, name): ...
    def uncordon(self, node): ...
    def pods(self): ...


class KubernetesScheduler:
    def pool_nodes(self, role):
        return k8s.pool_nodes(role)

    def available_workers(self):
        return k8s.available_worker_count()

    def gpu_demand(self, role):
        return k8s.get_current_gpu_pod_count(role)

    def ready(self, node):
        return k8s.node_ready(node)

    def drain(self, node):
        return k8s.drain_node(node)

    def deregister(self, name):
        k8s.deregister_k8s_node(name)

    def uncordon(self, node):
        k8s.v1.patch_node(node.metadata.name, {'spec': {'unschedulable': False}})

    def pods(self):
        return k8s.v1.list_pod_for_all_namespaces().items
