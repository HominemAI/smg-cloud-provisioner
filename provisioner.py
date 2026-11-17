"""Independent GPU and emergency pool reconciliation."""
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
import logging
import os
import threading
import time

import k8s

logger = logging.getLogger(__name__)


def enabled(name, default='false'):
    return os.getenv(name, default).lower() == 'true'


@dataclass
class Pool:
    name: str
    provider: object
    role: str
    max_nodes: int
    min_nodes: int = 0
    trigger_delay: float = 300
    recovery_delay: float = 900
    provisioning_timeout: float = 300
    unready_timeout: float = 600
    dry_run: bool = False
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

    def nodes(self):
        return [node for node in k8s.pool_nodes(self.role)
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
                if not k8s.drain_node(node):
                    return
            k8s.deregister_k8s_node(name)
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
            if self.name == 'emergency':
                self.emergency(now, instances, nodes)
            else:
                self.gpu(now, instances, nodes)

    def emergency(self, now, instances, nodes):
        # Spot termination can remove cloud capacity before Kubernetes removes its
        # node object. Reconcile that stale node so it cannot block replacement.
        missing = {name for name, node in nodes.items()
                   if name not in instances and not k8s.node_ready(node)}
        self.missing_nodes = {name: self.missing_nodes.get(name, now) for name in missing}
        for name, since in self.missing_nodes.items():
            if now - since >= self.provisioning_timeout:
                self.remove(name, nodes[name])
        workers = k8s.available_worker_count()
        logger.info('%sEmergency reconciliation: available_workers=%s instances=%s',
                    'DRY_RUN: ' if self.dry_run else '', workers, sorted(instances))
        if workers:
            if not instances and not self.creating and self.launch_time is None and hasattr(self.provider, 'reset_order'):
                self.provider.reset_order()
            self.outage_since = None
            if self.recovery_since is None:
                self.recovery_since = now
            if now - self.recovery_since >= self.recovery_delay:
                for name in instances | nodes.keys():
                    self.remove(name, nodes.get(name), drain=True)
                return
        else:
            self.recovery_since = None
            if self.outage_since is None:
                self.outage_since = now
            # A renewed outage cancels a partial recovery drain.
            for name in list(self.draining):
                node = nodes.get(name)
                if node and name not in self.deleting and not self.dry_run:
                    k8s.v1.patch_node(node.metadata.name, {'spec': {'unschedulable': False}})
                    self.draining.discard(name)

        for name in instances:
            node = nodes.get(name)
            if node and k8s.node_ready(node):
                self.observed[name] = now
            elif now - self.observed[name] >= self.provisioning_timeout:
                if hasattr(self.provider, 'advance_after_timeout'):
                    self.provider.advance_after_timeout(name)
                self.remove(name, node)
        if (not workers and now - self.outage_since >= self.trigger_delay
                and not instances and not nodes and self.max_nodes > 0
                and not self.creating and self.launch_time is None and not self.deleting):
            self.launch(now)

    def gpu(self, now, instances, nodes):
        for name, node in nodes.items():
            if name not in instances and not k8s.node_ready(node):
                self.missing_nodes.setdefault(name, now)
                if now - self.missing_nodes[name] >= self.provisioning_timeout:
                    self.remove(name, node)
            else:
                self.missing_nodes.pop(name, None)
        for name in instances - nodes.keys():
            if now - self.observed[name] >= self.provisioning_timeout:
                self.remove(name)
        # Preserve pod-count pressure and idle-only scale down, bounded per pool.
        desired = min(self.max_nodes, max(self.min_nodes, k8s.get_current_gpu_pod_count(self.role)))
        for name, node in nodes.items():
            if (not k8s.node_ready(node)
                    and time.time() - node.metadata.creation_timestamp.timestamp() > self.unready_timeout):
                self.remove(name, node)
        if len(nodes) < desired:
            self.recovery_since = None
            if (len(instances | nodes.keys()) < self.max_nodes and not self.creating
                    and self.launch_time is None and not self.deleting):
                self.launch(now)
        elif len(nodes) > desired:
            if self.recovery_since is None:
                self.recovery_since = now
            if now - self.recovery_since >= self.recovery_delay:
                pods = k8s.v1.list_pod_for_all_namespaces().items
                for name, node in nodes.items():
                    occupied = any(pod.spec.node_name == node.metadata.name
                                   and pod.status.phase not in ('Succeeded', 'Failed')
                                   and not any(owner.kind == 'DaemonSet' for owner in (pod.metadata.owner_references or []))
                                   for pod in pods)
                    if not occupied:
                        self.remove(name, node)
                        self.recovery_since = None
                        break
        else:
            self.recovery_since = None


def build_pools():
    pools = []
    common = dict(
        dry_run=enabled('DRY_RUN'),
        provisioning_timeout=int(os.getenv('SCALER_PROVISIONING_TIMEOUT', '300')),
        unready_timeout=int(os.getenv('UNREADY_NODE_TIMEOUT', '600')),
    )
    if enabled('GPU_ENABLED', 'true'):
        from gce import GCEProvider
        pools.append(Pool('gpu', GCEProvider(), k8s.NODE_ROLE_LABEL_VALUE,
                          int(os.getenv('SCALER_MAX_NODES', '2')),
                          min_nodes=int(os.getenv('SCALER_MIN_NODES', '0')),
                          recovery_delay=int(os.getenv('SCALER_DROP_DELAY', '900')), **common))
    if enabled('GPU_EC2_ENABLED'):
        from ec2 import EC2Provider
        role = os.getenv('GPU_EC2_ROLE', 'gpu-aws')
        script = os.getenv('GPU_EC2_STARTUP_SCRIPT') or (
            Path(__file__).parent / 'scripts' / 'gpu-ec2-startup.sh').read_text()
        script = script.replace('__GPU_EC2_ROLE__', role)
        provider = EC2Provider(
            template=os.environ['GPU_EC2_LAUNCH_TEMPLATE'],
            version=os.getenv('GPU_EC2_LAUNCH_TEMPLATE_VERSION', '$Default'),
            capacity_type=os.getenv('GPU_EC2_CAPACITY_TYPE', 'spot'),
            name_prefix=os.getenv('GPU_EC2_INSTANCE_NAME', 'smg-gpu-aws'),
            startup_script=script)
        pools.append(Pool('gpu-ec2', provider, role,
                          int(os.getenv('GPU_EC2_MAX_NODES', '1')),
                          min_nodes=0,
                          recovery_delay=int(os.getenv('SCALER_DROP_DELAY', '900')),
                          **{**common, 'provisioning_timeout': int(os.getenv('GPU_EC2_PROVISIONING_TIMEOUT', '1200')),
                             'unready_timeout': int(os.getenv('GPU_EC2_UNREADY_TIMEOUT', '1200'))}))
    if enabled('EMERGENCY_ENABLED'):
        order = os.getenv('EMERGENCY_PROVIDERS', os.getenv('EMERGENCY_PROVIDER', 'ec2,gce'))
        names = [name.strip() for name in order.split(',')]
        if len(set(names)) != len(names) or any(name not in ('gce', 'ec2') for name in names):
            raise ValueError('EMERGENCY_PROVIDERS must contain unique gce/ec2 entries in priority order')
        script = os.getenv('EMERGENCY_STARTUP_SCRIPT')
        if not script:
            raise ValueError('EMERGENCY_STARTUP_SCRIPT is required')
        prefix = os.getenv('EMERGENCY_INSTANCE_NAME', 'smg-emergency')
        providers = []
        for provider_name in names:
            settings = dict(name_prefix=f'{prefix}-{provider_name}', startup_script=script)
            if provider_name == 'gce':
                from gce import GCEProvider
                template = os.getenv('EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME')
                if not template:
                    raise ValueError('EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME is required')
                provider = GCEProvider(template=template, zone=os.getenv('EMERGENCY_GCP_ZONE') or os.getenv('GCP_ZONE'), **settings)
            else:
                from ec2 import EC2Provider
                provider = EC2Provider(**settings)
            providers.append(provider)
        from failover import OrderedProvider
        provider = OrderedProvider(providers, prefix)
        pools.append(Pool('emergency', provider, 'emergency', int(os.getenv('EMERGENCY_MAX_NODES', '1')),
                          trigger_delay=int(os.getenv('EMERGENCY_TRIGGER_DELAY', '300')),
                          recovery_delay=int(os.getenv('EMERGENCY_RECOVERY_DELAY', '900')), **common))
    for pool in pools:
        if (pool.max_nodes < pool.min_nodes or pool.min_nodes < 0
                or min(pool.trigger_delay, pool.recovery_delay, pool.provisioning_timeout, pool.unready_timeout) < 0):
            raise ValueError(f'Invalid limits or delays for {pool.name}')
    for left, right in combinations(pools, 2):
        # Inventory is scoped to a cloud provider. Nested prefixes in different
        # clouds cannot capture each other's instances; check concrete children
        # of ordered providers so same-cloud overlap still fails closed.
        for a in getattr(left.provider, 'providers', [left.provider]):
            for b in getattr(right.provider, 'providers', [right.provider]):
                same_name = a.NAME_PREFIX == b.NAME_PREFIX
                overlap = (a.NAME_PREFIX.startswith(b.NAME_PREFIX + '-')
                           or b.NAME_PREFIX.startswith(a.NAME_PREFIX + '-'))
                if same_name or (a.PROVIDER_NAME == b.PROVIDER_NAME and overlap):
                    raise ValueError('Pool instance prefixes must not overlap within a provider')
        if left.role == right.role:
            raise ValueError('Pool role labels must differ')
    return pools


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
