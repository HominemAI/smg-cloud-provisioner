"""Azure GPU VM adapter. Shared networking and identity are setup prerequisites."""
import base64
from dataclasses import dataclass
import re

from lifecycle import LifecycleStore
from managed_provider import ManagedProvider, InstanceState
from instance_audit import record


@dataclass(frozen=True)
class AzureSettings:
    subscription_id: str
    resource_group: str
    location: str
    subnet_id: str
    nsg_id: str
    vm_size: str
    image_id: str
    ssh_public_key: str
    capacity_type: str
    zone: str = ''
    public_ip: bool = False
    os_disk_gb: int = 128
    admin_username: str = 'smgadmin'
    operation_timeout: int = 900

    def __post_init__(self):
        required = ('subscription_id', 'resource_group', 'location', 'subnet_id',
                    'nsg_id', 'vm_size', 'image_id', 'ssh_public_key')
        if any(not getattr(self, key) for key in required):
            raise ValueError('All Azure identity, placement, network, image and SSH settings are required')
        if self.capacity_type not in ('spot', 'on-demand'):
            raise ValueError('Azure capacity_type must explicitly be spot or on-demand')
        if self.zone not in ('', '1', '2', '3'):
            raise ValueError('Azure zone must be empty or 1, 2, 3')
        if type(self.public_ip) is not bool or not 32 <= self.os_disk_gb <= 4095 or self.operation_timeout <= 0:
            raise ValueError('Invalid Azure IP, disk size or timeout setting')
        # Versioned gallery images or managed images; never an implicit latest image.
        if not self.image_id.startswith('/subscriptions/') or self.image_id.lower().endswith('/latest'):
            raise ValueError('Azure image_id must be an immutable managed image or gallery version resource ID')
        if '/images/' not in self.image_id or ('/galleries/' in self.image_id and '/versions/' not in self.image_id):
            raise ValueError('Use a managed image ID or a versioned gallery image ID')
        for resource_id, marker in ((self.subnet_id, '/subnets/'), (self.nsg_id, '/networkSecurityGroups/')):
            if not resource_id.lower().startswith(f'/subscriptions/{self.subscription_id}/'.lower()) or marker.lower() not in resource_id.lower():
                raise ValueError('Network resources must belong to the configured subscription')


class AzureDriver:
    def __init__(self, settings, prefix, compute, network):
        self.settings, self.prefix = settings, prefix
        self.compute, self.network = compute, network
        self.scope = f'azure/{settings.subscription_id}/{settings.resource_group}/{prefix}'.lower()
        self.root = f'/subscriptions/{settings.subscription_id}/resourceGroups/{settings.resource_group}/providers'

    def plan(self, name):
        ids = {'vm': f'{self.root}/Microsoft.Compute/virtualMachines/{name}',
               'nic': f'{self.root}/Microsoft.Network/networkInterfaces/{name}-nic',
               'disk': f'{self.root}/Microsoft.Compute/disks/{name}-os'}
        if self.settings.public_ip:
            ids['ip'] = f'{self.root}/Microsoft.Network/publicIPAddresses/{name}-ip'
        return ids

    def _ops(self, kind):
        return {'vm': self.compute.virtual_machines, 'disk': self.compute.disks,
                'nic': self.network.network_interfaces, 'ip': self.network.public_ip_addresses}[kind]

    def _get(self, kind, resource_id):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return self._ops(kind).get(self.settings.resource_group, resource_id.rsplit('/', 1)[1])
        except ResourceNotFoundError:
            return None

    def _wait(self, poller):
        result = poller.result(timeout=self.settings.operation_timeout)
        if not poller.done():
            raise TimeoutError('Azure operation is still pending')
        return result

    def ensure_absent(self, resources):
        for kind, resource_id in resources.items():
            if self._get(kind, resource_id) is not None:
                raise ValueError('Refusing to overwrite an existing Azure resource')

    def validate_script(self, script):
        if len(script.encode()) > 65535:
            raise ValueError('Startup script exceeds Azure custom-data limit')

    def validate(self):
        s = self.settings
        # SDK iterators follow every nextLink; an empty/failed page never implies capacity.
        for sku in self.compute.resource_skus.list(filter=f"location eq '{s.location}'"):
            if sku.resource_type != 'virtualMachines' or sku.name != s.vm_size:
                continue
            if s.location.lower() not in [v.lower() for v in (sku.locations or [])]:
                continue
            capabilities = {c.name: c.value for c in (sku.capabilities or [])}
            if s.capacity_type == 'spot' and capabilities.get('LowPriorityCapable', '').lower() != 'true':
                raise ValueError('Configured Azure size does not advertise Spot support')
            zones = {z for info in (sku.location_info or []) if info.location.lower() == s.location.lower()
                     for z in (info.zones or [])}
            if s.zone and s.zone not in zones:
                raise ValueError('Configured Azure size is not offered in this zone')
            # Conservative: any applicable subscription restriction prevents launch.
            for restriction in sku.restrictions or []:
                info = restriction.restriction_info
                locations = (getattr(info, 'locations', None) or restriction.values or [])
                if s.location.lower() in [v.lower() for v in locations]:
                    restricted_zones = getattr(info, 'zones', None) or []
                    if str(restriction.type).lower() == 'location' or not s.zone or s.zone in restricted_zones:
                        raise ValueError('Azure SKU is restricted for this subscription/placement')
            return
        raise ValueError('Configured Azure GPU size is not offered in this region')

    def _tags(self, name):
        return {'smgp-pool': self.prefix, 'smgp-instance-identifier': name, 'smgp-provider': 'azure'}

    def _owned(self, resource, name):
        tags = resource.tags or {}
        return all(tags.get(k) == v for k, v in self._tags(name).items())

    def inventory(self):
        result = {}
        for vm in self.compute.virtual_machines.list(self.settings.resource_group):
            if self._owned(vm, vm.name) and vm.name.startswith(self.prefix + '-'):
                result[vm.name] = {'Succeeded': InstanceState.READY, 'Failed': InstanceState.FAILED,
                                   'Canceled': InstanceState.FAILED}.get(vm.provisioning_state, InstanceState.PENDING)
        # Detect partial allocation even if the local journal was lost. Unknown
        # tagged resources block the pool; never adopt/delete them implicitly.
        for ops in (self.network.network_interfaces, self.network.public_ip_addresses):
            for resource in ops.list(self.settings.resource_group):
                name = (resource.tags or {}).get('smgp-instance-identifier', '')
                if self._owned(resource, name) and name.startswith(self.prefix + '-'):
                    result.setdefault(name, InstanceState.ORPHANED)
        # VM-created managed OS disks do not inherit tags. Their deterministic
        # names are an inventory alarm, never sufficient deletion authorization.
        pattern = re.compile(re.escape(self.prefix) + r'-[0-9a-f]{16}-os')
        for disk in self.compute.disks.list_by_resource_group(self.settings.resource_group):
            if pattern.fullmatch(disk.name):
                result.setdefault(disk.name[:-3], InstanceState.ORPHANED)
        return result

    def discover_resources(self, name, resources):
        return resources  # Azure IDs are known before submission.

    def create(self, name, resources, script, resources_changed=None):
        s = self.settings
        common = {'location': s.location, 'tags': self._tags(name)}
        record('azure_create_requested', 'azure', name, resources=resources,
               location=s.location, zone=s.zone, vm_size=s.vm_size, image_id=s.image_id,
               capacity_type=s.capacity_type)
        if 'ip' in resources:
            params = dict(common, sku={'name': 'Standard'}, public_ip_allocation_method='Static')
            if s.zone:
                params['zones'] = [s.zone]
            self._wait(self.network.public_ip_addresses.begin_create_or_update(s.resource_group, name + '-ip', params))
        ip_config = {'name': 'primary', 'subnet': {'id': s.subnet_id}, 'private_ip_allocation_method': 'Dynamic'}
        if 'ip' in resources:
            ip_config['public_ip_address'] = {'id': resources['ip'], 'delete_option': 'Delete'}
        self._wait(self.network.network_interfaces.begin_create_or_update(
            s.resource_group, name + '-nic', dict(common, ip_configurations=[ip_config],
                                                network_security_group={'id': s.nsg_id})))
        params = dict(common,
            hardware_profile={'vm_size': s.vm_size},
            storage_profile={'image_reference': {'id': s.image_id}, 'os_disk': {
                'name': name + '-os', 'create_option': 'FromImage', 'delete_option': 'Delete',
                'disk_size_gb': s.os_disk_gb, 'managed_disk': {'storage_account_type': 'Premium_LRS'}}},
            os_profile={'computer_name': name, 'admin_username': s.admin_username,
                'custom_data': base64.b64encode(script.encode()).decode(),
                'linux_configuration': {'disable_password_authentication': True,
                    'ssh': {'public_keys': [{'path': f'/home/{s.admin_username}/.ssh/authorized_keys',
                                           'key_data': s.ssh_public_key}]}}},
            network_profile={'network_interfaces': [{'id': resources['nic'], 'primary': True, 'delete_option': 'Delete'}]},
            priority='Spot' if s.capacity_type == 'spot' else 'Regular')
        if s.zone:
            params['zones'] = [s.zone]
        if s.capacity_type == 'spot':
            params.update(eviction_policy='Delete', billing_profile={'max_price': -1})
        self._wait(self.compute.virtual_machines.begin_create_or_update(s.resource_group, name, params))

    def delete(self, name, resources):
        # Validate all IDs before doing anything. Never follow arbitrary attached IDs.
        if resources != self.plan(name) or not re.fullmatch(re.escape(self.prefix) + r'-[0-9a-f]{16}', name):
            raise ValueError('Resource record does not match this pool and instance')
        for kind in ('vm', 'nic', 'ip', 'disk'):
            if kind not in resources:
                continue
            resource = self._get(kind, resources[kind])
            if resource is None:
                continue
            if kind != 'disk' and not self._owned(resource, name):
                raise ValueError('Refusing to delete an Azure resource without matching ownership tags')
            if kind == 'vm':
                storage = resource.storage_profile
                nic_ids = {nic.id.lower() for nic in resource.network_profile.network_interfaces}
                if (storage.data_disks or storage.os_disk.managed_disk.id.lower() != resources['disk'].lower()
                        or nic_ids != {resources['nic'].lower()}):
                    raise ValueError('VM attachments changed; refusing implicit deletion of untracked resources')
            if kind == 'disk' and (resource.managed_by or getattr(resource, 'managed_by_extended', None)):
                raise ValueError('Refusing to delete an attached OS disk')
            record('resource_delete_requested', 'azure', name, resource_id=resources[kind], kind=kind)
            self._wait(self._ops(kind).begin_delete(self.settings.resource_group, resources[kind].rsplit('/', 1)[1]))
            if self._get(kind, resources[kind]) is not None:
                raise RuntimeError('Azure resource deletion has not converged')
            record('resource_deleted', 'azure', name, resource_id=resources[kind], kind=kind)


class AzureProvider(ManagedProvider):
    PROVIDER_NAME = 'azure'

    def __init__(self, settings, ledger_path, compute=None, network=None, credential=None, **kwargs):
        if isinstance(settings, dict):
            settings = AzureSettings(**settings)
        prefix = kwargs.get('name_prefix', 'smg-gpu-azure')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}[a-z0-9]', prefix):
            raise ValueError('Azure pool prefix must be 2–32 lowercase letters/digits/hyphens')
        kwargs['name_prefix'] = prefix
        if compute is None or network is None:
            from azure.identity import EnvironmentCredential, WorkloadIdentityCredential
            from azure.mgmt.compute import ComputeManagementClient
            from azure.mgmt.network import NetworkManagementClient
            import os
            credential = credential or (WorkloadIdentityCredential() if os.getenv('AZURE_FEDERATED_TOKEN_FILE')
                                         else EnvironmentCredential())
            compute = compute or ComputeManagementClient(credential, settings.subscription_id)
            network = network or NetworkManagementClient(credential, settings.subscription_id)
        super().__init__(AzureDriver(settings, prefix, compute, network), LifecycleStore(ledger_path), **kwargs)
