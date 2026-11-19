"""Independent GPU and emergency pool reconciliation."""
from dataclasses import dataclass, field
import logging
import os
import threading
import time

import k8s
from scheduling import KubernetesScheduler, Scheduler
from scaling_policy import EmergencyPolicy, GpuPolicy, ScalingPolicy
from cloud_provider import CloudProvider

logger = logging.getLogger(__name__)


@dataclass
class Pool:
    name: str
    provider: CloudProvider
    role: str
    max_nodes: int
    min_nodes: int = 0
    trigger_delay: float = 300
    recovery_delay: float = 900
    provisioning_timeout: float = 300
    unready_timeout: float = 600
    dry_run: bool = False
    scheduler: Scheduler = field(default_factory=KubernetesScheduler)
    policy: ScalingPolicy | None = None
    clock: object = time.monotonic
    outage_since: float | None = None
    recovery_since: float | None = None
    observed: dict = field(default_factory=dict)
    missing_nodes: dict = field(default_factory=dict)
    creating: bool = False
    launching: str | None = None
    launch_time: float | None = None
    deleting: set = field(default_factory=set)
    draining: set = field(default_factory=set)
    lock: object = field(default_factory=threading.RLock)

    def __post_init__(self):
        if self.policy is None:
            self.policy = EmergencyPolicy() if self.name == "emergency" else GpuPolicy()

    def nodes(self):
        return [node for node in self.scheduler.pool_nodes(self.role)
                if (node.metadata.labels or {}).get(k8s.INSTANCE_PROVIDER_LABEL) in
                [p.PROVIDER_NAME for p in getattr(self.provider, "providers", [self.provider])]
                and (node.metadata.labels or {}).get(k8s.INSTANCE_IDENTIFIER_LABEL, '').startswith(
                    self.provider.NAME_PREFIX + '-')]

    def launch(self, now):
        logger.info('%sLaunch instance for %s', 'DRY_RUN: ' if self.dry_run else '', self.name)
        if self.dry_run:
            return
        self.creating = True
        self.launch_time = now

        def done(success, instance_name, error):
            with self.lock:
                self.creating = False
                # Keep the launch reservation until timeout even on errors: a lost
                # response is not proof that the cloud did not create the instance.
                logger.log(logging.INFO if success else logging.ERROR,
                           '%s creation %s: success=%s error=%s', self.name, instance_name, success, error)
        try:
            self.launching = self.provider.create_instance(callback=done)
        except Exception:
            self.creating = False
            raise

    def remove(self, name, node=None, drain=False):
        if name in self.deleting:
            return
        logger.info('%sRemove %s instance %s', 'DRY_RUN: ' if self.dry_run else '', self.name, name)
        if self.dry_run:
            return
        if node is not None:
            if drain:
                self.draining.add(name)
                if not self.scheduler.drain(node):
                    return
            self.scheduler.deregister(name)
        self.deleting.add(name)

        def done(success, instance_name, error):
            with self.lock:
                self.deleting.discard(instance_name)
                if success:
                    self.observed.pop(instance_name, None)
                    self.draining.discard(instance_name)
                logger.log(logging.INFO if success else logging.ERROR,
                           '%s deletion %s: success=%s error=%s', self.name, instance_name, success, error)
        try:
            self.provider.delete_instance(name, callback=done)
        except Exception:
            self.deleting.discard(name)
            raise

    def reconcile(self):
        with self.lock:
            now = self.clock()
            instances = set(self.provider.list_instances())
            nodes = {node.metadata.labels[k8s.INSTANCE_IDENTIFIER_LABEL]: node for node in self.nodes()}
            for name in instances:
                self.observed.setdefault(name, self.launch_time if name == self.launching else now)
            self.observed = {name: started for name, started in self.observed.items() if name in instances}
            if self.launching in instances or (
                not self.creating and self.launch_time is not None
                and now - self.launch_time >= self.provisioning_timeout
            ):
                self.launching = None
                self.launch_time = None
            self.policy.reconcile(self, now, instances, nodes)

def build_pools():
    from pool_configuration import build_pools as configure
    return configure()


def reconcile_pools(pools):
    for pool in pools:
        try:
            pool.reconcile()
        except Exception:
            logger.exception('Error reconciling pool %s', pool.name)


def start_auto_scaler(pools=None, shutdown=None):
    pools = build_pools() if pools is None else pools
    shutdown = shutdown or threading.Event()
    interval = int(os.getenv('SCALER_TIME_DELAY', '120'))
    if interval <= 0:
        raise ValueError('SCALER_TIME_DELAY must be positive')
    while not shutdown.is_set():
        try:
            k8s.update_nodes_list()
        except Exception:
            logger.exception('Cannot refresh nodes; skipping reconciliation')
        else:
            for pool in pools:
                try:
                    if pool.name != 'emergency':
                        k8s.update_pods_lists()
                    pool.reconcile()
                except Exception:
                    logger.exception('Error reconciling pool %s', pool.name)
        shutdown.wait(interval)
