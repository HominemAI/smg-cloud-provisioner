"""OVHcloud Public Cloud through OpenStack; no shared network or IAM mutations."""
import base64
from dataclasses import dataclass
import os

from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState
from provider_utils import check_record, unique, validate_prefix, wait_until


@dataclass(frozen=True)
class OVHSettings:
    project_id: str
    region: str
    auth_url: str
    flavor_id: str
    image_id: str
    network_id: str
    subnet_id: str
    security_group_ids: list[str]
    key_name: str
    capacity_type: str | None = None
    boot_disk_gb: int = 128
    volume_type: str = ''
    availability_zone: str = ''
    operation_timeout: int = 900

    def __post_init__(self):
        from urllib.parse import urlsplit
        if not all((self.project_id, self.region, self.flavor_id, self.image_id, self.network_id,
                    self.subnet_id, self.security_group_ids, self.key_name)):
            raise ValueError('OVH project, region, image/flavor, network, security groups and SSH key are required')
        if self.capacity_type not in (None, 'on-demand'):
            raise ValueError('Nova flavor/image specs do not define a Spot API; do not invent a capacity mode')
        if urlsplit(self.auth_url).scheme != 'https' or urlsplit(self.auth_url).username:
            raise ValueError('OpenStack authentication must use an HTTPS identity endpoint')
        if type(self.boot_disk_gb) is not int or self.boot_disk_gb < 32 or self.operation_timeout <= 0:
            raise ValueError('Invalid OVH disk size or timeout')


class OVHDriver:
    def __init__(self, settings, prefix, connection):
        self.settings, self.prefix, self.connection = settings, prefix, connection
        self.scope = f'ovhcloud/{settings.project_id}/{settings.region}/{prefix}'

    def plan(self, name):
        return {'name': name, 'project_id': self.settings.project_id, 'region': self.settings.region}

    def _metadata(self, name):
        return {'smgp_pool': self.prefix, 'smgp_instance': name, 'smgp_provider': 'ovhcloud'}

    def _check_project(self):
        if self.connection.session.get_project_id() != self.settings.project_id:
            raise ValueError('OpenStack credentials target a different project')

    def _owned(self, resource, name):
        return (resource.project_id == self.settings.project_id
                and all((resource.metadata or {}).get(k) == v for k, v in self._metadata(name).items()))

    def _port_owned(self, port, name):
        return port.project_id == self.settings.project_id and port.description == f'smgp:{self.prefix}:{name}'

    def _inventory(self):
        self._check_project()
        servers = list(self.connection.compute.servers(details=True))
        volumes = list(self.connection.block_storage.volumes(details=True))
        ports = list(self.connection.network.ports(project_id=self.settings.project_id))
        return servers, volumes, ports

    def inventory(self):
        servers, volumes, ports = self._inventory()
        result = {}
        for server in servers:
            if (server.metadata or {}).get('smgp_pool') != self.prefix:
                continue
            if not self._owned(server, server.name) or server.name in result:
                raise UnresolvedLaunch('Ambiguous OpenStack server ownership')
            result[server.name] = {'ACTIVE': InstanceState.READY, 'ERROR': InstanceState.FAILED,
                                   'SHUTOFF': InstanceState.FAILED}.get(server.status, InstanceState.PENDING)
        for volume in volumes:
            name = (volume.metadata or {}).get('smgp_instance', '')
            if self._owned(volume, name):
                result.setdefault(name, InstanceState.ORPHANED)
        for port in ports:
            marker = f'smgp:{self.prefix}:'
            if (port.description or '').startswith(marker):
                result.setdefault(port.description[len(marker):], InstanceState.ORPHANED)
        return result

    def discover_resources(self, name, resources):
        servers, volumes, ports = self._inventory()
        found = {'server_id': unique(s for s in servers if self._owned(s, name)),
                 'volume_id': unique(v for v in volumes if self._owned(v, name)),
                 'port_id': unique(p for p in ports if self._port_owned(p, name))}
        return resources | {k: value.id for k, value in found.items() if value is not None}

    def ensure_absent(self, resources):
        servers, volumes, ports = self._inventory()
        name = resources['name']
        if any(r.name in (name, name + '-root', name + '-port') for r in servers + volumes + ports):
            raise ValueError('An OpenStack resource already uses this reserved name')

    def validate_script(self, script):
        if len(script.encode()) > 65535:
            raise ValueError('OpenStack user data exceeds the supported limit')

    def validate(self):
        s, c = self.settings, self.connection
        self._check_project()
        flavor = c.compute.get_flavor(s.flavor_id)
        image = c.image.get_image(s.image_id)
        if image.status != 'active' or image.min_disk > s.boot_disk_gb:
            raise ValueError('Pinned image is unavailable or larger than the boot disk')
        network = c.network.get_network(s.network_id)
        subnet = c.network.get_subnet(s.subnet_id)
        if subnet.network_id != network.id:
            raise ValueError('Subnet does not belong to configured network')
        for identifier in s.security_group_ids:
            group = c.network.get_security_group(identifier)
            if group.project_id != s.project_id:
                raise ValueError('Security group belongs to another project')
        c.compute.get_keypair(s.key_name)

    def create(self, name, resources, script, resources_changed):
        s, c = self.settings, self.connection
        params = dict(name=name + '-root', size=s.boot_disk_gb, image_id=s.image_id, metadata=self._metadata(name))
        if s.volume_type:
            params['volume_type'] = s.volume_type
        volume = c.block_storage.create_volume(**params)
        resources_changed({'volume_id': volume.id})
        c.block_storage.wait_for_status(volume, status='available', failures=['error'], wait=s.operation_timeout)
        port = c.network.create_port(name=name + '-port', network_id=s.network_id,
            fixed_ips=[{'subnet_id': s.subnet_id}], security_group_ids=s.security_group_ids,
            description=f'smgp:{self.prefix}:{name}')
        resources_changed({'port_id': port.id})
        params = dict(name=name, flavor_id=s.flavor_id, key_name=s.key_name,
            metadata=self._metadata(name), networks=[{'port': port.id}],
            user_data=base64.b64encode(script.encode()).decode(),
            block_device_mapping=[{'uuid': volume.id, 'source_type': 'volume', 'destination_type': 'volume',
                                   'boot_index': 0, 'delete_on_termination': False}])
        if s.availability_zone:
            params['availability_zone'] = s.availability_zone
        server = c.compute.create_server(**params)
        resources_changed({'server_id': server.id})
        c.compute.wait_for_server(server, status='ACTIVE', failures=['ERROR'], wait=s.operation_timeout)

    def _get(self, method, identifier):
        from openstack.exceptions import NotFoundException
        try:
            return method(identifier)
        except NotFoundException:
            return None

    def delete(self, name, resources):
        self._check_project()
        check_record(self.prefix, name, resources, self.plan(name))
        c, s = self.connection, self.settings
        discovered = self.discover_resources(name, resources)
        if discovered != resources:
            raise UnresolvedLaunch('Persist discovered OpenStack IDs before cleanup')
        if resources.get('server_id'):
            server = self._get(c.compute.get_server, resources['server_id'])
            if server:
                if not self._owned(server, name):
                    raise ValueError('Server ownership changed')
                if {v['id'] for v in server.attached_volumes or []} - {resources.get('volume_id')}:
                    raise ValueError('Server has untracked volumes')
                if {p.port_id for p in c.compute.server_interfaces(server)} != {resources.get('port_id')}:
                    raise ValueError('Server has untracked network ports')
                c.compute.delete_server(server)
                c.compute.wait_for_delete(server, wait=s.operation_timeout)
        if resources.get('port_id'):
            port = self._get(c.network.get_port, resources['port_id'])
            if port:
                if not self._port_owned(port, name) or port.device_id:
                    raise ValueError('Port ownership changed or it is still attached')
                c.network.delete_port(port)
                wait_until(lambda: self._get(c.network.get_port, port.id) is None, s.operation_timeout)
        if resources.get('volume_id'):
            volume = self._get(c.block_storage.get_volume, resources['volume_id'])
            if volume:
                if not self._owned(volume, name) or volume.attachments:
                    raise ValueError('Volume ownership changed or it is still attached')
                c.block_storage.delete_volume(volume)
                c.block_storage.wait_for_delete(volume, wait=s.operation_timeout)


class OVHProvider(ManagedProvider):
    PROVIDER_NAME = 'ovhcloud'

    def __init__(self, settings, ledger_path, connection=None, **kwargs):
        if isinstance(settings, dict):
            settings = OVHSettings(**settings)
        prefix = kwargs.setdefault('name_prefix', 'smg-gpu-ovh')
        validate_prefix(prefix)
        if connection is None:
            from openstack.connection import Connection
            from keystoneauth1.identity.v3 import ApplicationCredential
            from keystoneauth1.session import Session
            identifier = os.environ['OS_APPLICATION_CREDENTIAL_ID']
            secret = os.environ['OS_APPLICATION_CREDENTIAL_SECRET']
            auth = ApplicationCredential(auth_url=settings.auth_url, application_credential_id=identifier,
                                         application_credential_secret=secret)
            connection = Connection(session=Session(auth=auth, timeout=30), region_name=settings.region,
                                    interface='public', connect_retries=0, status_code_retries=0)
        super().__init__(OVHDriver(settings, prefix, connection), LifecycleStore(ledger_path), **kwargs)
