"""Nebius GPU instances using the official SDK and durable resource ownership."""
from dataclasses import dataclass
import os
from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState
from provider_utils import check_record, unique, validate_prefix


@dataclass(frozen=True)
class NebiusSettings:
    project_id: str
    subnet_id: str
    image_id: str
    platform: str
    preset: str
    security_group_ids: list[str]
    capacity_type: str = 'on-demand'
    purchase_spec: dict | None = None
    public_ip: bool = False
    boot_disk_gb: int = 128
    operation_timeout: int = 900

    def __post_init__(self):
        if not all((self.project_id, self.subnet_id, self.image_id, self.platform,
                    self.preset, self.security_group_ids)):
            raise ValueError('Nebius project, subnet, image, platform/preset and security groups are required')
        if self.capacity_type not in ('on-demand', 'preemptible'):
            raise ValueError('Use on-demand, preemptible, or a native purchase_spec')
        if self.purchase_spec is not None:
            if not isinstance(self.purchase_spec, dict) or len(self.purchase_spec) != 1 or not set(self.purchase_spec) <= {'on_demand', 'preemptible', 'follows_spot_price', 'spot_pricing_policy'}:
                raise ValueError('purchase_spec requires exactly one native Nebius purchasing field')
        if type(self.public_ip) is not bool or type(self.boot_disk_gb) is not int or self.boot_disk_gb < 32 or self.operation_timeout <= 0:
            raise ValueError('Invalid Nebius network, disk or timeout settings')


class NebiusDriver:
    def __init__(self, settings, prefix, sdk):
        import nebius.api.nebius.compute.v1 as compute
        import nebius.api.nebius.vpc.v1 as vpc
        self.n = compute
        self.v = vpc
        self.settings, self.prefix = settings, prefix
        self.instances = self.n.InstanceServiceClient(sdk)
        self.disks = self.n.DiskServiceClient(sdk)
        self.images = self.n.ImageServiceClient(sdk)
        self.platforms = self.n.PlatformServiceClient(sdk)
        self.subnets = self.v.SubnetServiceClient(sdk)
        self.groups = self.v.SecurityGroupServiceClient(sdk)
        self.scope = f'nebius/{settings.project_id}/{prefix}'

    def plan(self, name):
        return {'name': name, 'project_id': self.settings.project_id}

    def _labels(self, name):
        return {'smgp-pool': self.prefix, 'smgp-instance': name, 'smgp-provider': 'nebius'}

    def _metadata(self, name, suffix=''):
        from nebius.api.nebius.common.v1 import ResourceMetadata
        return ResourceMetadata(parent_id=self.settings.project_id, name=name + suffix, labels=self._labels(name))

    def _owned(self, obj, name):
        return obj.metadata.parent_id == self.settings.project_id and all(obj.metadata.labels.get(k) == v for k, v in self._labels(name).items())

    def _call(self, method, request):
        return method(request, retries=0, timeout=60).wait()

    def _list(self, client, request_type):
        token, seen, result = '', set(), []
        while True:
            response = self._call(client.list, request_type(parent_id=self.settings.project_id, page_size=100, page_token=token))
            result.extend(response.items)
            token = response.next_page_token
            if not token:
                return result
            if token in seen:
                raise ValueError('Repeated Nebius pagination token')
            seen.add(token)

    def _inventory(self):
        return self._list(self.instances, self.n.ListInstancesRequest), self._list(self.disks, self.n.ListDisksRequest)

    def inventory(self):
        instances, disks = self._inventory()
        result = {}
        for obj in instances:
            name = obj.metadata.labels.get('smgp-instance', '')
            if obj.metadata.labels.get('smgp-pool') != self.prefix:
                continue
            if not self._owned(obj, name) or name in result:
                raise UnresolvedLaunch('Ambiguous Nebius ownership')
            result[name] = {4: InstanceState.READY, 6: InstanceState.FAILED, 8: InstanceState.FAILED}.get(int(obj.status.state), InstanceState.PENDING)
        for obj in disks:
            name = obj.metadata.labels.get('smgp-instance', '')
            if self._owned(obj, name):
                result.setdefault(name, InstanceState.ORPHANED)
        return result

    def discover_resources(self, name, resources):
        instances, disks = self._inventory()
        found = {'instance_id': unique(x for x in instances if self._owned(x, name)),
                 'disk_id': unique(x for x in disks if self._owned(x, name))}
        return resources | {k: obj.metadata.id for k, obj in found.items() if obj is not None}

    def ensure_absent(self, resources):
        instances, disks = self._inventory()
        if any(x.metadata.name in (resources['name'], resources['name'] + '-root') for x in instances + disks):
            raise ValueError('Reserved Nebius name already exists')

    def validate_script(self, script):
        if len(script.encode()) > 65535:
            raise ValueError('Cloud-init exceeds adapter limit')

    def validate(self):
        s = self.settings
        self._purchase()  # Reject malformed native settings before any cloud write.
        platforms = self._list(self.platforms, self.n.ListPlatformsRequest)
        platform = unique(p for p in platforms if p.metadata.name == s.platform)
        if platform is None or not any(p.name == s.preset for p in platform.spec.presets):
            raise ValueError('Configured platform/preset is not available')
        subnet = self._call(self.subnets.get, self.v.GetSubnetRequest(id=s.subnet_id))
        if subnet.metadata.parent_id != s.project_id:
            raise ValueError('Subnet is outside configured project')
        self._call(self.images.get, self.n.GetImageRequest(id=s.image_id))
        for identifier in s.security_group_ids:
            group = self._call(self.groups.get, self.v.GetSecurityGroupRequest(id=identifier))
            if group.metadata.parent_id != s.project_id:
                raise ValueError('Security group is outside configured project')

    def _operation(self, method, request, key=None, changed=None):
        operation = self._call(method, request)
        if changed:
            changed({key + '_operation_id': operation.id})
            if operation.resource_id:
                changed({key + '_id': operation.resource_id})
        operation.sync_wait(timeout=self.settings.operation_timeout)
        if not operation.successful():
            raise RuntimeError('Nebius operation failed')
        if changed:
            if not operation.resource_id:
                raise UnresolvedLaunch('Successful operation did not return resource identity')
            changed({key + '_id': operation.resource_id})
        return operation.resource_id

    def _purchase(self):
        n, s = self.n, self.settings
        values = s.purchase_spec or ({'preemptible': {'on_preemption': 1}} if s.capacity_type == 'preemptible' else {'on_demand': {}})
        types = {'on_demand': n.OnDemandSpec, 'preemptible': n.PreemptibleSpec,
                 'follows_spot_price': n.FollowsSpotPriceSpec, 'spot_pricing_policy': n.SpotPricingPolicySpec}
        return {key: types[key](**value) for key, value in values.items()}

    def create(self, name, resources, script, resources_changed):
        n, s = self.n, self.settings
        disk = self._operation(self.disks.create, n.CreateDiskRequest(metadata=self._metadata(name, '-root'),
            spec=n.DiskSpec(size_gibibytes=s.boot_disk_gb, type=n.DiskSpec.DiskType.NETWORK_SSD,
                            source_image_id=s.image_id)), 'disk', resources_changed)
        nic = dict(name='eth0', subnet_id=s.subnet_id, ip_address=n.IPAddress(),
                   security_groups=[n.SecurityGroup(id=x) for x in s.security_group_ids])
        if s.public_ip:
            nic['public_ip_address'] = n.PublicIPAddress(static=False)
        self._operation(self.instances.create, n.CreateInstanceRequest(metadata=self._metadata(name),
            spec=n.InstanceSpec(resources=n.ResourcesSpec(platform=s.platform, preset=s.preset),
                boot_disk=n.AttachedDiskSpec(attach_mode=n.AttachedDiskSpec.AttachMode.READ_WRITE,
                                             existing_disk=n.ExistingDisk(id=disk)),
                network_interfaces=[n.NetworkInterfaceSpec(**nic)], cloud_init_user_data=script,
                hostname=name, **self._purchase())), 'instance', resources_changed)

    def _get(self, client, request):
        from grpc import StatusCode
        from nebius.aio.service_error import RequestError
        try:
            return self._call(client.get, request)
        except RequestError as exc:
            if exc.status.code == StatusCode.NOT_FOUND:
                return None
            raise

    def delete(self, name, resources):
        check_record(self.prefix, name, resources, self.plan(name))
        if self.discover_resources(name, resources) != resources:
            raise UnresolvedLaunch('Persist discovered Nebius IDs before cleanup')
        n = self.n
        if resources.get('instance_id'):
            obj = self._get(self.instances, n.GetInstanceRequest(id=resources['instance_id']))
            if obj is not None:
                if not self._owned(obj, name) or obj.spec.secondary_disks or obj.spec.filesystems or obj.spec.boot_disk.existing_disk.id != resources.get('disk_id'):
                    raise ValueError('Instance ownership or attachments changed')
                self._operation(self.instances.delete, n.DeleteInstanceRequest(id=obj.metadata.id))
        if resources.get('disk_id'):
            obj = self._get(self.disks, n.GetDiskRequest(id=resources['disk_id']))
            if obj is not None:
                if not self._owned(obj, name) or obj.status.read_write_attachment or obj.status.read_only_attachments:
                    raise ValueError('Disk ownership changed or disk remains attached')
                self._operation(self.disks.delete, n.DeleteDiskRequest(id=obj.metadata.id))


class NebiusProvider(ManagedProvider):
    PROVIDER_NAME = 'nebius'

    def __init__(self, settings, ledger_path, sdk=None, **kwargs):
        if isinstance(settings, dict):
            settings = NebiusSettings(**settings)
        prefix = kwargs.setdefault('name_prefix', 'smg-gpu-nebius')
        validate_prefix(prefix)
        if sdk is None:
            from nebius.sdk import SDK
            sdk = SDK(credentials_file_name=os.environ['NEBIUS_CREDENTIALS_FILE'])
        super().__init__(NebiusDriver(settings, prefix, sdk), LifecycleStore(ledger_path), **kwargs)
