"""Configuration/composition root, separate from reconciliation policy."""
import json
import os
import re
from itertools import combinations

import k8s
from provider_factory import factory
from scaling_policy import GpuPolicy, EmergencyPolicy


def enabled(name, default='false'):
    return os.getenv(name, default).lower() == 'true'


def gpu_provider(spec):
    """Compose user-selected placements under one role and one capacity limit."""
    if 'candidates' not in spec:
        if not {'provider', 'options'} <= set(spec):
            raise ValueError('GPU pool requires provider/options or candidates')
        factory.validate_pool(spec['provider'], spec.get('max_nodes', 1))
        return factory.create_gpu(spec['provider'], spec['role'], **dict(spec['options']))
    if 'provider' in spec or 'options' in spec:
        raise ValueError('Use candidates or provider/options, not both')
    from placement import PlacementProvider
    if not spec.get('placement_ledger_path'):
        raise ValueError('Ordered placements require a persistent placement_ledger_path')
    prefix = spec.get('name_prefix', spec['name'])
    if not isinstance(prefix, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,19}[a-z0-9]', prefix):
        raise ValueError('Candidate pool prefix must be 2–21 lowercase letters/digits/hyphens')
    candidates = spec['candidates']
    if not isinstance(candidates, list) or not candidates:
        raise ValueError('candidates must be a nonempty ordered list')
    providers, identifiers = [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {'id', 'provider', 'options'}:
            raise ValueError('Each candidate needs id, provider and options')
        identifier = candidate['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9]{0,9}', identifier) or identifier in identifiers:
            raise ValueError('Candidate IDs must be unique 1–10 lowercase alphanumeric identifiers')
        identifiers.add(identifier)
        options = dict(candidate['options'])
        if 'name_prefix' in options:
            raise ValueError('Candidate prefixes derive from pool prefix and stable candidate ID')
        options['name_prefix'] = prefix + '-' + identifier
        factory.validate_pool(candidate['provider'], spec.get('max_nodes', 1))
        providers.append(factory.create_gpu(candidate['provider'], spec['role'], **options))
    return PlacementProvider(providers, prefix, spec['placement_ledger_path'])


def build_pools():
    from provisioner import Pool
    pools = []
    configured = os.getenv('GPU_POOLS_JSON')
    common = dict(
        dry_run=enabled('DRY_RUN'),
        provisioning_timeout=int(os.getenv('SCALER_PROVISIONING_TIMEOUT', '300')),
        unready_timeout=int(os.getenv('UNREADY_NODE_TIMEOUT', '600')),
    )
    if configured is None and enabled('GPU_ENABLED', 'true'):
        pools.append(Pool('gpu', factory.create('gce'), k8s.NODE_ROLE_LABEL_VALUE,
                          int(os.getenv('SCALER_MAX_NODES', '2')),
                          min_nodes=int(os.getenv('SCALER_MIN_NODES', '0')),
                          recovery_delay=int(os.getenv('SCALER_DROP_DELAY', '900')), **common))
    if configured is None and enabled('GPU_EC2_ENABLED'):
        role = os.getenv('GPU_EC2_ROLE', 'gpu-aws')
        script = os.getenv('GPU_EC2_STARTUP_SCRIPT')
        if script:
            script = script.replace('__GPU_EC2_ROLE__', role)
        provider = factory.create_gpu('ec2', role,
            template=os.environ['GPU_EC2_LAUNCH_TEMPLATE'],
            version=os.getenv('GPU_EC2_LAUNCH_TEMPLATE_VERSION', '$Default'),
            capacity_type=os.getenv('GPU_EC2_CAPACITY_TYPE'),
            name_prefix=os.getenv('GPU_EC2_INSTANCE_NAME', 'smg-gpu-aws'),
            startup_script=script)
        pools.append(Pool('gpu-ec2', provider, role,
                          int(os.getenv('GPU_EC2_MAX_NODES', '1')),
                          min_nodes=0,
                          recovery_delay=int(os.getenv('SCALER_DROP_DELAY', '900')),
                          **{**common, 'provisioning_timeout': int(os.getenv('GPU_EC2_PROVISIONING_TIMEOUT', '1200')),
                             'unready_timeout': int(os.getenv('GPU_EC2_UNREADY_TIMEOUT', '1200'))}))
    if configured is not None:
        specs = json.loads(configured)
        if not isinstance(specs, list):
            raise ValueError('GPU_POOLS_JSON must be a list')
        for spec in specs:
            if not isinstance(spec, dict) or type(spec.get('enabled', False)) is not bool:
                raise ValueError('Each GPU pool must be an object with a boolean enabled flag')
            if spec.get('enabled', False) is not True:
                continue
            allowed = {'enabled', 'provider', 'options', 'role', 'name', 'max_nodes',
                       'provisioning_timeout', 'unready_timeout', 'candidates', 'name_prefix', 'placement_ledger_path'}
            if set(spec) - allowed or not {'role', 'name'} <= set(spec):
                raise ValueError('GPU pool has missing or unknown settings')
            if spec['name'] == 'emergency':
                raise ValueError('The emergency pool name is reserved')
            if any(type(spec.get(key, 1)) is not int or spec.get(key, 1) < 0
                   for key in ('max_nodes', 'provisioning_timeout', 'unready_timeout')):
                raise ValueError('GPU pool limits and timeouts must be nonnegative integers')
            role = spec['role']
            provider = gpu_provider(spec)
            pools.append(Pool(spec['name'], provider, role, spec.get('max_nodes', 1), policy=GpuPolicy(),
                              recovery_delay=int(os.getenv('SCALER_DROP_DELAY', '900')),
                              **{**common, 'provisioning_timeout': spec.get('provisioning_timeout', 1200),
                                 'unready_timeout': spec.get('unready_timeout', 1200)}))
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
                template = os.getenv('EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME')
                if not template:
                    raise ValueError('EMERGENCY_GCP_INSTANCE_TEMPLATE_NAME is required')
                provider = factory.create('gce', template=template, zone=os.getenv('EMERGENCY_GCP_ZONE') or os.getenv('GCP_ZONE'), **settings)
            else:
                provider = factory.create('ec2', **settings)
            providers.append(provider)
        from failover import OrderedProvider
        provider = OrderedProvider(providers, prefix)
        pools.append(Pool('emergency', provider, 'emergency', int(os.getenv('EMERGENCY_MAX_NODES', '1')),
                          policy=EmergencyPolicy(),
                          trigger_delay=int(os.getenv('EMERGENCY_TRIGGER_DELAY', '300')),
                          recovery_delay=int(os.getenv('EMERGENCY_RECOVERY_DELAY', '900')), **common))
    if len({pool.name for pool in pools}) != len(pools):
        raise ValueError('Pool names must be unique')
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
