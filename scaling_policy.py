"""Scaling decisions independent of cloud APIs and Kubernetes client construction."""
import logging
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class ScalingPolicy(Protocol):
    def reconcile(self, pool, now, instances, nodes): ...


class EmergencyPolicy:
    def reconcile(self, pool, now, instances, nodes):
        # Spot termination can remove cloud capacity before Kubernetes removes its
        # node object. Reconcile that stale node so it cannot block replacement.
        missing = {name for name, node in nodes.items()
                   if name not in instances and not pool.scheduler.ready(node)}
        pool.missing_nodes = {name: pool.missing_nodes.get(name, now) for name in missing}
        for name, since in pool.missing_nodes.items():
            if now - since >= pool.provisioning_timeout:
                pool.remove(name, nodes[name])
        workers = pool.scheduler.available_workers()
        logger.info('%sEmergency reconciliation: available_workers=%s instances=%s',
                    'DRY_RUN: ' if pool.dry_run else '', workers, sorted(instances))
        if workers:
            if not instances and not pool.creating and pool.launch_time is None and hasattr(pool.provider, 'reset_order'):
                pool.provider.reset_order()
            pool.outage_since = None
            if pool.recovery_since is None:
                pool.recovery_since = now
            if now - pool.recovery_since >= pool.recovery_delay:
                for name in instances | nodes.keys():
                    pool.remove(name, nodes.get(name), drain=True)
                return
        else:
            pool.recovery_since = None
            if pool.outage_since is None:
                pool.outage_since = now
            # A renewed outage cancels a partial recovery drain.
            for name in list(pool.draining):
                node = nodes.get(name)
                if node and name not in pool.deleting and not pool.dry_run:
                    pool.scheduler.uncordon(node)
                    pool.draining.discard(name)

        for name in instances:
            node = nodes.get(name)
            if node and pool.scheduler.ready(node):
                pool.observed[name] = now
            elif now - pool.observed[name] >= pool.provisioning_timeout:
                if hasattr(pool.provider, 'advance_after_timeout'):
                    pool.provider.advance_after_timeout(name)
                pool.remove(name, node)
        if (not workers and now - pool.outage_since >= pool.trigger_delay
                and not instances and not nodes and pool.max_nodes > 0
                and not pool.creating and pool.launch_time is None and not pool.deleting):
            pool.launch(now)

class GpuPolicy:
    def reconcile(self, pool, now, instances, nodes):
        for name, node in nodes.items():
            if name not in instances and not pool.scheduler.ready(node):
                pool.missing_nodes.setdefault(name, now)
                if now - pool.missing_nodes[name] >= pool.provisioning_timeout:
                    pool.remove(name, node)
            else:
                pool.missing_nodes.pop(name, None)
        for name in instances - nodes.keys():
            if now - pool.observed[name] >= pool.provisioning_timeout:
                pool.remove(name)
        # Preserve pod-count pressure and idle-only scale down, bounded per pool.
        desired = min(pool.max_nodes, max(pool.min_nodes, pool.scheduler.gpu_demand(pool.role)))
        for name, node in nodes.items():
            if (not pool.scheduler.ready(node)
                    and time.time() - node.metadata.creation_timestamp.timestamp() > pool.unready_timeout):
                pool.remove(name, node)
        if len(nodes) < desired:
            pool.recovery_since = None
            if (len(instances | nodes.keys()) < desired and not pool.creating
                    and pool.launch_time is None and not pool.deleting):
                pool.launch(now)
        elif len(nodes) > desired:
            if pool.recovery_since is None:
                pool.recovery_since = now
            if now - pool.recovery_since >= pool.recovery_delay:
                pods = pool.scheduler.pods()
                for name, node in nodes.items():
                    occupied = any(pod.spec.node_name == node.metadata.name
                                   and pod.status.phase not in ('Succeeded', 'Failed')
                                   and not any(owner.kind == 'DaemonSet' for owner in (pod.metadata.owner_references or []))
                                   for pod in pods)
                    if not occupied:
                        pool.remove(name, node)
                        pool.recovery_since = None
                        break
        else:
            pool.recovery_since = None
