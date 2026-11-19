"""Scaleway local-SSD GPU instances; SBS-backed types deliberately unsupported."""
from dataclasses import dataclass
import os
import re
from uuid import UUID
import requests
from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState
from provider_utils import check_record, unique, validate_prefix, wait_until


@dataclass(frozen=True)
class ScalewaySettings:
    project_id: str
    zone: str
    commercial_type: str
    image_id: str
    security_group_id: str
    capacity_type: str | None = None
    boot_disk_gb: int = 128
    operation_timeout: int = 900

    def __post_init__(self):
        for value in (self.project_id, self.image_id, self.security_group_id):
            UUID(value)
        if not re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]+', self.zone) or not self.commercial_type:
            raise ValueError('Explicit Scaleway zone/type required')
        if self.capacity_type not in (None, 'on-demand'):
            raise ValueError('Use the configured Scaleway offering; a Spot request field is not implemented')
        if type(self.boot_disk_gb) is not int or self.boot_disk_gb < 32 or self.operation_timeout <= 0:
            raise ValueError('Invalid Scaleway disk size or timeout')


class ScalewayAPI:
    def __init__(self, secret, zone):
        self.session = requests.Session()
        self.session.headers['X-Auth-Token'] = secret
        self.base = f'https://api.scaleway.com/instance/v1/zones/{zone}/'

    def request(self, method, path, **kwargs):
        response = self.session.request(method, self.base + path, timeout=60, allow_redirects=False, **kwargs)
        if response.status_code == 404 and method == 'GET':
            return None
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f'Scaleway request failed (HTTP {response.status_code})')
        return response.json() if response.content else {}

    def list(self, path, key, **params):
        result, page = [], 1
        while True:
            response = self.request('GET', path, params=params | {'page': page, 'per_page': 100})
            batch = response[key]
            if isinstance(batch, dict):
                result.extend(batch.items())
            else:
                result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1


class ScalewayDriver:
    def __init__(self, settings, prefix, api):
        self.settings, self.prefix, self.api = settings, prefix, api
        self.scope = f'scaleway/{settings.project_id}/{settings.zone}/{prefix}'

    def plan(self, name):
        return {'name': name, 'project_id': self.settings.project_id, 'zone': self.settings.zone}

    def _tags(self, name):
        return [f'smgp-pool={self.prefix}', f'smgp-instance={name}', 'smgp-provider=scaleway']

    def _owned(self, obj, name):
        return obj.get('project') == self.settings.project_id and set(self._tags(name)) <= set(obj.get('tags', []))

    def _inventory(self):
        return [self.api.list(kind, kind, project=self.settings.project_id) for kind in ('servers', 'volumes', 'ips')]

    def _volume_owned(self, obj, name):
        # Inline root volumes have no tag field; require exact reserved name/project.
        return obj.get('project') == self.settings.project_id and obj.get('name') == name + '-root'

    def inventory(self):
        servers, volumes, ips = self._inventory()
        result = {}
        for obj in servers:
            if f'smgp-pool={self.prefix}' not in obj.get('tags', []):
                continue
            name = obj['name']
            if not self._owned(obj, name) or name in result:
                raise UnresolvedLaunch('Ambiguous Scaleway ownership')
            result[name] = {'running': InstanceState.READY, 'stopped': InstanceState.FAILED,
                            'locked': InstanceState.FAILED}.get(obj['state'], InstanceState.PENDING)
        for obj in volumes:
            if re.fullmatch(re.escape(self.prefix) + r'-[0-9a-f]{16}-root', obj['name']):
                result.setdefault(obj['name'][:-5], InstanceState.ORPHANED)
        for obj in ips:
            for tag in obj.get('tags', []):
                if tag.startswith('smgp-instance=') and self._owned(obj, tag.split('=', 1)[1]):
                    result.setdefault(tag.split('=', 1)[1], InstanceState.ORPHANED)
        return result

    def discover_resources(self, name, resources):
        servers, volumes, ips = self._inventory()
        found = {'server_id': unique(x for x in servers if self._owned(x, name)),
                 'volume_id': unique(x for x in volumes if self._volume_owned(x, name)),
                 'ip_id': unique(x for x in ips if self._owned(x, name))}
        return resources | {k: obj['id'] for k, obj in found.items() if obj is not None}

    def ensure_absent(self, resources):
        servers, volumes, _ = self._inventory()
        if any(x['name'] in (resources['name'], resources['name'] + '-root') for x in servers + volumes):
            raise ValueError('Reserved Scaleway name already exists')

    def validate_script(self, script):
        if len(script.encode()) > 65535:
            raise ValueError('Cloud-init exceeds adapter limit')

    def validate(self):
        s = self.settings
        types = dict(self.api.list('products/servers', 'servers'))
        sku = types.get(s.commercial_type, {})
        if not sku:
            raise ValueError('Configured Scaleway type is not in this zone')
        constraints = sku.get('per_volume_constraint', {}).get('l_ssd')
        size = s.boot_disk_gb * 10**9
        if not constraints or not constraints.get('min_size', 0) <= size <= constraints.get('max_size', 0):
            raise ValueError('Type does not support configured local SSD; SBS is not implemented')
        image = self.api.request('GET', 'images/' + s.image_id)['image']
        if image.get('extra_volumes') or image['root_volume']['volume_type'] != 'l_ssd':
            raise ValueError('Pinned image must have exactly one local SSD root volume')
        group = self.api.request('GET', 'security_groups/' + s.security_group_id)['security_group']
        if group['project'] != s.project_id:
            raise ValueError('Security group is outside project')

    def create(self, name, resources, script, resources_changed):
        s, a = self.settings, self.api
        ip = a.request('POST', 'ips', json={'project': s.project_id, 'tags': self._tags(name), 'type': 'routed_ipv4'})['ip']
        resources_changed({'ip_id': ip['id']})
        server = a.request('POST', 'servers', json={'name': name, 'project': s.project_id,
            'commercial_type': s.commercial_type, 'image': s.image_id, 'tags': self._tags(name),
            'security_group': s.security_group_id, 'protected': False, 'dynamic_ip_required': False,
            'routed_ip_enabled': True, 'public_ips': [ip['id']], 'enable_ipv6': False,
            'volumes': {'0': {'name': name + '-root', 'size': s.boot_disk_gb * 10**9, 'volume_type': 'l_ssd'}}})['server']
        resources_changed({'server_id': server['id']})
        volume = server['volumes']['0']
        resources_changed({'volume_id': volume['id']})
        path = 'servers/' + server['id']
        a.request('PATCH', path + '/user_data/cloud-init', data=script.encode(), headers={'Content-Type': 'text/plain'})
        a.request('POST', path + '/action', json={'action': 'poweron'})
        wait_until(lambda: a.request('GET', path)['server']['state'] == 'running', s.operation_timeout)

    def delete(self, name, resources):
        check_record(self.prefix, name, resources, self.plan(name))
        if self.discover_resources(name, resources) != resources:
            raise UnresolvedLaunch('Persist discovered Scaleway IDs before cleanup')
        a, s = self.api, self.settings
        if resources.get('server_id'):
            path = 'servers/' + resources['server_id']
            response = a.request('GET', path)
            if response:
                obj = response['server']
                if not self._owned(obj, name) or {v['id'] for v in obj['volumes'].values()} - {resources.get('volume_id')} or obj.get('private_nics') or obj.get('filesystems'):
                    raise ValueError('Server ownership or attachments changed')
                if {ip['id'] for ip in obj.get('public_ips', [])} - {resources.get('ip_id')}:
                    raise ValueError('Server has untracked public addresses')
                if obj['state'] != 'stopped':
                    a.request('POST', path + '/action', json={'action': 'poweroff'})
                    wait_until(lambda: a.request('GET', path)['server']['state'] == 'stopped', s.operation_timeout)
                a.request('DELETE', path)
                wait_until(lambda: a.request('GET', path) is None, s.operation_timeout)
        for kind, key, singular in [('volumes', 'volume_id', 'volume'), ('ips', 'ip_id', 'ip')]:
            if not resources.get(key):
                continue
            path = kind + '/' + resources[key]
            response = a.request('GET', path)
            if response:
                obj = response[singular]
                owned = self._volume_owned(obj, name) if kind == 'volumes' else self._owned(obj, name)
                if not owned or obj.get('server'):
                    raise ValueError('Resource ownership changed or resource is attached')
                a.request('DELETE', path)
                wait_until(lambda: a.request('GET', path) is None, s.operation_timeout)


class ScalewayProvider(ManagedProvider):
    PROVIDER_NAME = 'scaleway'

    def __init__(self, settings, ledger_path, api=None, **kwargs):
        if isinstance(settings, dict):
            settings = ScalewaySettings(**settings)
        prefix = kwargs.setdefault('name_prefix', 'smg-gpu-scw')
        validate_prefix(prefix)
        api = api or ScalewayAPI(os.environ['SCW_SECRET_KEY'], settings.zone)
        super().__init__(ScalewayDriver(settings, prefix, api), LifecycleStore(ledger_path), **kwargs)
