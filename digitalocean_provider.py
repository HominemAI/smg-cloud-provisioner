"""Single-GPU DigitalOcean Droplets using the shared durable lifecycle."""
from dataclasses import dataclass
import os
import re
import time
import uuid

from digitalocean_api import DigitalOceanAPI
from instance_audit import record
from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState


@dataclass(frozen=True)
class DigitalOceanSettings:
    account_uuid: str
    region: str
    size: str
    image_id: int
    vpc_uuid: str
    firewall_id: str
    project_id: str
    ssh_keys: list
    capacity_type: str | None = None
    public_networking: bool = False
    operation_timeout: int = 900
    poll_interval: int = 5

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9-]{8,128}', self.account_uuid):
            raise ValueError('Configure the account/team identifier from GET /v2/account')
        for key in ('vpc_uuid', 'firewall_id', 'project_id'):
            uuid.UUID(getattr(self, key))
        if not re.fullmatch(r'[a-z]+[0-9]+', self.region):
            raise ValueError('Specify an exact DigitalOcean datacenter slug')
        if not re.fullmatch(r'[a-zA-Z0-9-]+', self.size):
            raise ValueError('Specify a DigitalOcean size slug')
        if self.capacity_type is None:
            object.__setattr__(self, 'capacity_type', 'spot' if self.size.endswith('-spot') else 'on-demand')
        if self.capacity_type not in ('spot', 'on-demand') or self.size.endswith('-spot') != (self.capacity_type == 'spot'):
            raise ValueError('DigitalOcean size slug must match the explicit capacity_type')
        if type(self.image_id) is not int or self.image_id <= 0:
            raise ValueError('Pin a positive numeric DigitalOcean image ID')
        if (not isinstance(self.ssh_keys, list) or not self.ssh_keys
                or any(not ((type(key) is int and key > 0) or
                            (isinstance(key, str) and re.fullmatch(r'(?:[0-9a-f]{2}:){15}[0-9a-f]{2}', key)))
                       for key in self.ssh_keys)):
            raise ValueError('Configure existing SSH key IDs or fingerprints')
        if (type(self.public_networking) is not bool or type(self.operation_timeout) is not int
                or self.operation_timeout <= 0 or type(self.poll_interval) is not int
                or not 1 <= self.poll_interval <= 30):
            raise ValueError('Invalid DigitalOcean network or timeout setting')


class DigitalOceanDriver:
    def __init__(self, settings, prefix, api, clock=time.monotonic, sleep=time.sleep):
        self.settings, self.prefix, self.api = settings, prefix, api
        self.clock, self.sleep = clock, sleep
        self.scope = f'digitalocean/{settings.account_uuid}/{prefix}'
        self.pool_tag = f'smgp-pool:{prefix}'

    def _check_account(self):
        account = self.api.request('GET', '/v2/account')['account']
        identity = (account.get('team') or {}).get('uuid') or account['uuid']
        if identity != self.settings.account_uuid or account['status'] != 'active':
            raise ValueError('DigitalOcean token is not for the configured active account')

    def _tags(self, name):
        return [self.pool_tag, 'smgp-provider:digitalocean', f'smgp-instance:{name}']

    def plan(self, name):
        return {'name': name, 'account_uuid': self.settings.account_uuid,
                'region': self.settings.region, 'pool_tag': self.pool_tag,
                'instance_tag': f'smgp-instance:{name}'}

    def _owned(self, droplet, name):
        return (droplet.get('name') == name and set(self._tags(name)) <= set(droplet.get('tags') or [])
                and droplet.get('region', {}).get('slug') == self.settings.region)

    def _droplets(self):
        self._check_account()
        return list(self.api.pages('/v2/droplets', 'droplets', tag_name=self.pool_tag))

    def inventory(self):
        result = {}
        for droplet in self._droplets():
            name = droplet['name']
            if not self._owned(droplet, name) or not re.fullmatch(re.escape(self.prefix) + r'-[0-9a-f]{16}', name):
                raise UnresolvedLaunch('Pool-tagged Droplet has unexpected ownership or region')
            if name in result:
                raise UnresolvedLaunch('Multiple DigitalOcean Droplets have the same owned name')
            result[name] = {'active': InstanceState.READY, 'off': InstanceState.FAILED}.get(
                droplet['status'], InstanceState.PENDING)
        return result

    def ensure_absent(self, resources):
        # Name collisions without ownership tags must not be overwritten/adopted.
        self._check_account()
        if any(d['name'] == resources['name'] for d in self.api.pages('/v2/droplets', 'droplets')):
            raise ValueError('A DigitalOcean Droplet already uses this instance name')

    def validate_script(self, script):
        if len(script.encode()) > 65536:
            raise ValueError('DigitalOcean user data exceeds 64 KiB')

    def validate(self):
        self._check_account()
        s = self.settings
        sizes = list(self.api.pages('/v2/sizes', 'sizes'))
        size = next((item for item in sizes if item['slug'] == s.size), None)
        if size is None or not size['available'] or s.region not in size['regions']:
            raise ValueError('Exact GPU size is not available in the configured DigitalOcean region')
        image = self.api.request('GET', f'/v2/images/{s.image_id}')['image']
        if image['status'] != 'available' or s.region not in image['regions']:
            raise ValueError('Pinned DigitalOcean image is not available in this region')
        vpc = self.api.request('GET', f'/v2/vpcs/{s.vpc_uuid}')['vpc']
        if vpc['region'] != s.region:
            raise ValueError('DigitalOcean VPC and Droplet regions differ')
        firewall = self.api.request('GET', f'/v2/firewalls/{s.firewall_id}')['firewall']
        if self.pool_tag not in firewall['tags'] or firewall['inbound_rules'] or not firewall['outbound_rules']:
            raise ValueError('Preconfigure a pool-tagged firewall with no inbound and explicit outbound rules')
        if firewall.get('status') != 'succeeded':
            raise ValueError('DigitalOcean firewall is not ready')
        self.api.request('GET', f'/v2/projects/{s.project_id}')
        record('digitalocean_preflight', 'digitalocean', self.prefix, size=s.size,
               region=s.region, image_id=s.image_id, capacity_type=s.capacity_type,
               quoted_price_hourly=size.get('price_hourly'))

    @staticmethod
    def _identity(droplet):
        identifier = droplet.get('id')
        if type(identifier) is not int or identifier <= 0:
            raise RuntimeError('Missing DigitalOcean Droplet ID')
        return {'droplet_id': identifier, 'droplet_urn': f'do:droplet:{identifier}'}

    def _get(self, identifier):
        data = self.api.request('GET', f'/v2/droplets/{identifier}', missing_ok=True)
        return data['droplet'] if data is not None else None

    def discover_resources(self, name, resources):
        if 'droplet_id' in resources:
            droplet = self._get(resources['droplet_id'])
            if droplet is not None and not self._owned(droplet, name):
                raise ValueError('Recorded Droplet ownership changed')
            return resources
        matches = [d for d in self._droplets() if d['name'] == name]
        if len(matches) > 1:
            raise UnresolvedLaunch('Ambiguous Droplet identity; manual reconciliation required')
        if not matches:
            return resources
        if not self._owned(matches[0], name):
            raise ValueError('Discovered Droplet ownership does not match')
        return resources | self._identity(matches[0])

    def _wait(self, predicate):
        deadline = self.clock() + self.settings.operation_timeout
        while True:
            if predicate():
                return
            if self.clock() >= deadline:
                raise TimeoutError('DigitalOcean operation has not converged')
            self.sleep(self.settings.poll_interval)

    def create(self, name, resources, script, resources_changed):
        s = self.settings
        self._check_account()
        record('digitalocean_create_requested', 'digitalocean', name, size=s.size,
               region=s.region, image_id=s.image_id, capacity_type=s.capacity_type)
        data = self.api.request('POST', '/v2/droplets', body={
            'name': name, 'region': s.region, 'size': s.size, 'image': s.image_id,
            'ssh_keys': s.ssh_keys, 'vpc_uuid': s.vpc_uuid, 'public_networking': s.public_networking,
            'ipv6': False, 'backups': False, 'monitoring': False, 'with_droplet_agent': False,
            'tags': self._tags(name), 'user_data': script})
        droplet = data['droplet']
        identity = self._identity(droplet)
        # Persist before project assignment, polling, or any further request.
        resources_changed(identity)
        actions = [a['id'] for a in data.get('links', {}).get('actions', [])]
        if any(type(identifier) is not int or identifier <= 0 for identifier in actions):
            raise RuntimeError('Invalid DigitalOcean action identifier')
        if actions:
            resources_changed({'action_ids': actions})
        if not self._owned(droplet, name):
            raise ValueError('Created Droplet ownership is unexpected')
        self.api.request('POST', f'/v2/projects/{s.project_id}/resources', body={'resources': [identity['droplet_urn']]})
        for identifier in actions:
            def action_done():
                action = self.api.request('GET', f'/v2/actions/{identifier}')['action']
                if action['status'] == 'errored':
                    raise RuntimeError('DigitalOcean create action failed')
                return action['status'] == 'completed'
            self._wait(action_done)
        def active():
            current = self._get(identity['droplet_id'])
            if current is None:
                return False
            if not self._owned(current, name):
                raise ValueError('Droplet ownership changed while provisioning')
            return current['status'] == 'active'
        self._wait(active)

    def delete(self, name, resources):
        self._check_account()
        if (not re.fullmatch(re.escape(self.prefix) + r'-[0-9a-f]{16}', name)
                or any(resources.get(key) != value for key, value in self.plan(name).items())):
            raise ValueError('Resource record belongs to a different pool')
        if 'droplet_id' not in resources:
            # An explicitly abandoned submission can have no returned ID. Do not
            # quietly delete an unrecorded Droplet discovered during cleanup.
            if any(d['name'] == name for d in self._droplets()):
                raise UnresolvedLaunch('Reconcile and persist the Droplet ID before cleanup')
            return
        identifier = resources['droplet_id']
        if self._identity({'id': identifier})['droplet_urn'] != resources.get('droplet_urn'):
            raise ValueError('Droplet ID and URN do not match')
        droplet = self._get(identifier)
        if droplet is None:
            return
        if not self._owned(droplet, name):
            raise ValueError('Refusing deletion without matching Droplet ownership')
        if droplet.get('volume_ids') or droplet.get('backup_ids') or droplet.get('snapshot_ids'):
            raise ValueError('Droplet has separately billed attachments/backups; review cleanup manually')
        record('resource_delete_requested', 'digitalocean', name, droplet_id=identifier)
        self.api.request('DELETE', f'/v2/droplets/{identifier}', missing_ok=True)
        self._wait(lambda: self._get(identifier) is None)
        record('resource_deleted', 'digitalocean', name, droplet_id=identifier)


class DigitalOceanProvider(ManagedProvider):
    PROVIDER_NAME = 'digitalocean'

    def __init__(self, settings, ledger_path, api=None, **kwargs):
        if isinstance(settings, dict):
            settings = DigitalOceanSettings(**settings)
        prefix = kwargs.get('name_prefix', 'smg-gpu-do')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}[a-z0-9]', prefix):
            raise ValueError('DigitalOcean prefix must be 2–32 lowercase letters/digits/hyphens')
        kwargs['name_prefix'] = prefix
        api = api or DigitalOceanAPI(os.getenv('DIGITALOCEAN_TOKEN'))
        super().__init__(DigitalOceanDriver(settings, prefix, api), LifecycleStore(ledger_path), **kwargs)
