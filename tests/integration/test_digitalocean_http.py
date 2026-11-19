import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.parse import parse_qs

import requests

from digitalocean_api import DigitalOceanAPI, DigitalOceanAPIError
from digitalocean_provider import DigitalOceanProvider, DigitalOceanSettings
from lifecycle import UnresolvedLaunch
from support import JSONServer, completion

ROOT = Path(__file__).resolve().parents[2]
COMMIT = '6ee1980060d9ec8ce9f9853edf22768ca61cdf43'
UID = '12345678-1234-1234-1234-123456789012'
ACCOUNT = 'localaccount123456789'


class DropletSimulator:
    """Stateful lifecycle fixture; Prism independently checks the create wire format."""
    def __init__(self, prism_url):
        self.requests, self.droplets = [], {}
        self.prism_url = prism_url
        self.validated_creates = 0
        self.polls = 0
        self.lose_reply = False
        self.fail_page = False
        self.reject_create = False

    def handle(self, method, path, query, body, headers):
        if headers.get('Authorization') != 'Bearer local-fake-token':
            return 401, {}, {}
        if path == '/v2/account':
            return 200, {'account': {'uuid': ACCOUNT, 'status': 'active'}}, {}
        if path == '/v2/sizes':
            return 200, {'sizes': [{'slug': 'gpu-b300x1-288gb-lc-spot', 'available': True,
                'regions': ['mkc1'], 'price_hourly': 6.17, 'gpu_info': {'count': 1, 'model': 'nvidia_b300'}}]}, {}
        if path.startswith('/v2/images/'):
            return 200, {'image': {'status': 'available', 'regions': ['mkc1']}}, {}
        if path.startswith('/v2/vpcs/'):
            return 200, {'vpc': {'region': 'mkc1'}}, {}
        if path.startswith('/v2/firewalls/'):
            return 200, {'firewall': {'tags': ['smgp-pool:gpu-do'], 'inbound_rules': [],
                'outbound_rules': [{'protocol': 'tcp'}], 'status': 'succeeded'}}, {}
        if path.startswith('/v2/projects/'):
            return 200, {}, {}
        if method == 'GET' and path == '/v2/droplets':
            if 'page' not in parse_qs(query):
                return 200, {'droplets': [], 'links': {'pages': {'next': self.url + '/v2/droplets?page=2'}}}, {}
            if self.fail_page:
                return 503, {}, {}
            return 200, {'droplets': list(self.droplets.values())}, {}
        if method == 'POST' and path == '/v2/droplets':
            if self.reject_create:
                return 422, {'message': 'Injected quota rejection'}, {}
            session = requests.Session()
            session.trust_env = False
            try:
                schema = session.post(self.prism_url + path, json=body,
                    headers={'Authorization': 'Bearer local-fake-token'}, timeout=10, allow_redirects=False)
            finally:
                session.close()
            if schema.status_code != 202:
                return schema.status_code, schema.json(), {}
            self.validated_creates += 1
            self.droplets[777] = dict(id=777, name=body['name'], tags=body['tags'],
                region={'slug': body['region']}, status='new', volume_ids=[], snapshot_ids=[], backup_ids=[])
            if self.lose_reply:
                self.droplets[777]['status'] = 'active'
                return 503, {}, {}
            return 202, {'droplet': self.droplets[777], 'links': {'actions': [{'id': 888}]}}, {}
        if path == '/v2/actions/888':
            self.polls += 1
            if self.polls > 1:
                self.droplets[777]['status'] = 'active'
            return 200, {'action': {'status': 'completed' if self.polls > 1 else 'in-progress'}}, {}
        if path == '/v2/droplets/777':
            if method == 'DELETE':
                self.droplets.pop(777, None)
                return 204, None, {}
            return (200, {'droplet': self.droplets[777]}, {}) if 777 in self.droplets else (404, {}, {})
        return 404, {}, {}


class DigitalOceanHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo = ROOT / '.cache/emulators/digitalocean-openapi'
        prism = ROOT / 'tests/integration/node_modules/.bin/prism'
        if not prism.exists() or not repo.exists():
            raise RuntimeError('Run sh scripts/setup_emulators.sh first')
        revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != COMMIT:
            raise RuntimeError('DigitalOcean OpenAPI checkout must match the pinned revision')
        subprocess.run(['git', '-C', str(repo), 'diff', '--exit-code', COMMIT, '--', 'specification'], check=True,
                       stdout=subprocess.DEVNULL)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.prism_url = f'http://127.0.0.1:{port}'
        cls.log = tempfile.TemporaryFile(mode='w+b')
        cls.addClassCleanup(cls.log.close)
        env = {key: value for key, value in os.environ.items() if key in ('PATH', 'TMPDIR', 'SYSTEMROOT')}
        cls.process = subprocess.Popen([str(prism), 'mock', str(repo / 'specification/DigitalOcean-public.v2.yaml'),
            '--host', '127.0.0.1', '--port', str(port), '--errors', '--verboseLevel', 'error'],
            stdout=cls.log, stderr=subprocess.STDOUT, env=env)
        def stop():
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)
        cls.addClassCleanup(stop)
        session = requests.Session()
        session.trust_env = False
        cls.addClassCleanup(session.close)
        for _ in range(150):
            if cls.process.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(cls.log.read().decode()[-2000:])
            try:
                session.get(cls.prism_url + '/v2/account', timeout=1)
                return
            except requests.ConnectionError:
                time.sleep(0.1)
        raise RuntimeError('Prism did not start within 15 seconds')

    def setUp(self):
        self.app = DropletSimulator(self.prism_url)
        self.server = JSONServer(self.app)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = str(Path(self.temp.name) / 'state.sqlite')
        self.provider = self.new_provider()

    def new_provider(self):
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        api = DigitalOceanAPI('local-fake-token', session, base_url=self.server.url)
        config = DigitalOceanSettings(account_uuid=ACCOUNT, region='mkc1', size='gpu-b300x1-288gb-lc-spot',
            image_id=123, vpc_uuid=UID, firewall_id=UID, project_id=UID, ssh_keys=[456], capacity_type='spot',
            poll_interval=1, operation_timeout=5)
        return DigitalOceanProvider(config, self.ledger, api=api, name_prefix='gpu-do',
                                    startup_script='#!/bin/sh\necho {{INSTANCE_NAME}}')

    def test_provider_payload_validated_by_official_schema_then_lifecycle(self):
        name, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'], str(result['error']))
        self.assertEqual(self.app.validated_creates, 1)
        self.assertGreaterEqual(self.app.polls, 2)
        self.assertEqual(self.provider.list_instances(), [name])
        entry, = self.provider.store.records(self.provider.scope)
        self.assertEqual(entry['resources']['droplet_id'], 777)
        self.assertEqual(entry['resources']['action_ids'], [888])
        _, deleted = completion(self, lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(deleted['success'])
        self.assertEqual(self.provider.list_instances(), [])

    def test_prism_rejects_malformed_actual_http_payload(self):
        session = requests.Session()
        session.trust_env = False
        self.addCleanup(session.close)
        api = DigitalOceanAPI('local-fake-token', session, base_url=self.prism_url)
        with self.assertRaises(DigitalOceanAPIError) as error:
            api.request('POST', '/v2/droplets', body={'name': 'invalid', 'size': 123, 'image': []})
        self.assertEqual(error.exception.status, 422)

    def test_lost_reply_restart_and_eviction(self):
        self.app.lose_reply = True
        name, result = completion(self, self.provider.create_instance)
        self.assertFalse(result['success'])
        restarted = self.new_provider()
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(restarted.store.records(restarted.scope)[0]['resources']['droplet_id'], 777)
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(self.app.validated_creates, 1)
        self.app.droplets.clear()
        self.assertEqual(restarted.list_instances(), [name])
        _, result = completion(self, lambda **kw: restarted.delete_instance(name, **kw))
        self.assertTrue(result['success'])
        self.assertEqual(restarted.list_instances(), [])

    def test_quota_and_pagination_failure_do_not_launch_again(self):
        self.app.fail_page = True
        with self.assertRaises(DigitalOceanAPIError):
            self.provider.create_instance()
        self.app.fail_page = False
        self.app.reject_create = True
        _, result = completion(self, self.provider.create_instance)
        self.assertFalse(result['success'])
        with self.assertRaises(UnresolvedLaunch):
            self.provider.create_instance()
        self.assertEqual(sum(m == 'POST' and p == '/v2/droplets' for m, p, _ in self.app.requests), 1)
