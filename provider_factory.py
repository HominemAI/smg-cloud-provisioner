"""Lazy provider registry: adding a cloud adapter does not change the scaler."""
from importlib import import_module
from dataclasses import dataclass

from bootstrap import gpu_bootstrap


@dataclass(frozen=True)
class ProviderCapabilities:
    """Implemented behavior, not a claim about live regional capacity."""
    market_types: frozenset[str]
    durable_resources: bool = False
    max_pool_nodes: int | None = None
    placement: str = 'zone'
    interruption_events: bool = False


@dataclass(frozen=True)
class ProviderRegistration:
    builder: object
    capabilities: ProviderCapabilities
    gpu_script: object = None


class ProviderFactory:
    def __init__(self):
        self._builders = {}

    def register(self, name, builder, capabilities=None, gpu_script=None):
        if name in self._builders:
            raise ValueError(f'Provider already registered: {name}')
        self._builders[name] = ProviderRegistration(builder, capabilities or ProviderCapabilities(frozenset()), gpu_script)

    def registration(self, name):
        if name not in self._builders:
            raise ValueError(f'Unsupported provider: {name}')
        return self._builders[name]

    def create(self, name, **settings):
        return self.registration(name).builder(**settings)

    def validate_pool(self, name, max_nodes):
        limit = self.registration(name).capabilities.max_pool_nodes
        if limit is not None and max_nodes > limit:
            raise ValueError(f'{name} currently supports at most {limit} node(s) per pool')

    def create_gpu(self, name, role, **settings):
        registration = self.registration(name)
        if not settings.get('startup_script') and registration.gpu_script:
            settings['startup_script'] = registration.gpu_script(role)
        return registration.builder(**settings)


def lazy(module, name):
    return lambda **settings: getattr(import_module(module), name)(**settings)


factory = ProviderFactory()
factory.register('gce', lazy('gce', 'GCEProvider'), ProviderCapabilities(frozenset({'template'})))
factory.register('ec2', lazy('ec2', 'EC2Provider'),
                 ProviderCapabilities(frozenset({'spot', 'on-demand'}), placement='regional-fleet'),
                 gpu_script=lambda role: gpu_bootstrap('gpu-ec2-prepare.sh', role))
factory.register('azure', lazy('azure_provider', 'AzureProvider'),
                 ProviderCapabilities(frozenset({'spot', 'on-demand'}), durable_resources=True,
                                      placement='regional-or-zone'),
                 gpu_script=lambda role: gpu_bootstrap('gpu-azure-prepare.sh', role))
factory.register('digitalocean', lazy('digitalocean_provider', 'DigitalOceanProvider'),
                 ProviderCapabilities(frozenset({'spot', 'on-demand'}), durable_resources=True,
                                      placement='datacenter'),
                 gpu_script=lambda role: gpu_bootstrap('gpu-digitalocean-prepare.sh', role))

for provider, module, builder, placement in (
    ('scaleway', 'scaleway_provider', 'ScalewayProvider', 'zone'),
    ('nebius', 'nebius_provider', 'NebiusProvider', 'project-subnet'),
    ('ovhcloud', 'ovhcloud_provider', 'OVHProvider', 'region-or-zone'),
):
    factory.register(provider, lazy(module, builder),
                     ProviderCapabilities(frozenset({'on-demand', 'preemptible', 'native-spec'} if provider == 'nebius' else {'offering'}), durable_resources=True,
                                          placement=placement),
                     gpu_script=lambda role: gpu_bootstrap('gpu-preinstalled-prepare.sh', role))
