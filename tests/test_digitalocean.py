import copy
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock

from digitalocean_api import DigitalOceanAPI, DigitalOceanAPIError
from digitalocean_provider import DigitalOceanSettings, DigitalOceanDriver, DigitalOceanProvider
from lifecycle import UnresolvedLaunch
from managed_provider import InstanceState


ACCOUNT = 'testaccount123456789'
UID = '12345678-1234-1234-1234-123456789012'


def settings(**kwargs):
    return DigitalOceanSettings(**(dict(account_uuid=ACCOUNT, region='mkc1',
        size='gpu-b300x1-288gb-lc-spot', image_id=123, vpc_uuid=UID, firewall_id=UID,
        project_id=UID, ssh_keys=[456], capacity_type='spot', poll_interval=1) | kwargs))


class FakeAPI:
    def __init__(self):
        self.droplets, self.calls = {}, []
        self.lose_response = False
        self.action_status = 'completed'
        self.account = {'uuid': ACCOUNT, 'status': 'active'}
        self.size = {'slug': settings().size, 'available': True, 'regions': ['mkc1'],
                     'price_hourly': 6.17, 'gpu_info': {'count': 1, 'model': 'nvidia_b300'}}
        self.firewall = {'tags': ['smgp-pool:gpu-do'], 'inbound_rules': [],
                         'outbound_rules': [{'protocol': 'tcp'}], 'status': 'succeeded'}

    def request(self, method, path, *, body=None, params=None, missing_ok=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == '/v2/account':
            return {'account': self.account}
        if path.startswith('/v2/images/'):
            return {'image': {'status': 'available', 'regions': ['mkc1']}}
        if path.startswith('/v2/vpcs/'):
            return {'vpc': {'region': 'mkc1'}}
        if path.startswith('/v2/firewalls/'):
            return {'firewall': self.firewall}
        if path.startswith('/v2/projects/'):
            return {}
        if path.startswith('/v2/actions/'):
            return {'action': {'status': self.action_status}}
        if method == 'POST' and path == '/v2/droplets':
            d = {'id': 777, 'name': body['name'], 'tags': body['tags'], 'region': {'slug': body['region']},
                 'status': 'active', 'volume_ids': [], 'backup_ids': [], 'snapshot_ids': []}
            self.droplets[777] = d
            if self.lose_response:
                raise TimeoutError('simulated lost response')
            return {'droplet': d, 'links': {'actions': [{'id': 888}]}}
        if path.startswith('/v2/droplets/'):
            identifier = int(path.rsplit('/', 1)[1])
            if method == 'DELETE':
                self.droplets.pop(identifier, None)
                return {}
            return {'droplet': self.droplets[identifier]} if identifier in self.droplets else None
        raise AssertionError((method, path))

    def pages(self, path, key, **params):
        if path == '/v2/sizes':
            yield self.size
        elif path == '/v2/droplets':
            yield from [d for d in self.droplets.values()
                        if not params.get('tag_name') or params['tag_name'] in d['tags']]
        else:
            raise AssertionError(path)


class DigitalOceanTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'state.sqlite')
        self.provider = self.new_provider()

    def new_provider(self):
        return DigitalOceanProvider(settings(), self.path, api=self.api, name_prefix='gpu-do',
                                    startup_script='echo {{INSTANCE_NAME}}')

    def complete(self, action):
        done, output = threading.Event(), {}
        def callback(**kwargs):
            output.update(kwargs)
            done.set()
        name = action(callback=callback)
        self.assertTrue(done.wait(3))
        return name, output

    def test_create_tracks_ids_before_project_assignment_and_delete(self):
        original = self.api.request
        def request(method, path, **kwargs):
            if method == 'POST' and path.endswith('/resources'):
                entry, = self.provider.store.records(self.provider.scope)
                self.assertEqual(entry['resources']['droplet_id'], 777)
                self.assertEqual(entry['resources']['action_ids'], [888])
            return original(method, path, **kwargs)
        self.api.request = request
        name, result = self.complete(self.provider.create_instance)
        self.assertTrue(result['success'])
        self.assertEqual(self.provider.list_instances(), [name])
        payload = next(body for method, path, body in self.api.calls if method == 'POST' and path == '/v2/droplets')
        self.assertEqual(payload['size'], settings().size)
        self.assertFalse(payload['public_networking'])
        self.assertFalse(payload['backups'])
        self.assertNotIn('volumes', payload)
        _, deleted = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(deleted['success'])
        self.assertEqual(self.provider.list_instances(), [])
        self.assertEqual(self.api.droplets, {})

    def test_lost_response_recovers_id_after_restart_without_second_post(self):
        self.api.lose_response = True
        name, result = self.complete(self.provider.create_instance)
        self.assertFalse(result['success'])
        restarted = self.new_provider()
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(restarted.store.records(restarted.scope)[0]['resources']['droplet_id'], 777)
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(sum(m == 'POST' and p == '/v2/droplets' for m, p, _ in self.api.calls), 1)

    def test_persisted_id_survives_action_failure(self):
        self.api.action_status = 'errored'
        _, result = self.complete(self.provider.create_instance)
        self.assertFalse(result['success'])
        self.assertEqual(self.provider.store.records(self.provider.scope)[0]['resources']['droplet_id'], 777)

    def test_uncertain_absence_still_blocks(self):
        self.api.lose_response = True
        self.complete(self.provider.create_instance)
        self.api.droplets.clear()
        with self.assertRaises(UnresolvedLaunch):
            self.new_provider().list_instances()

    def test_ownership_duplicate_names_and_inventory_failure(self):
        name, _ = self.complete(self.provider.create_instance)
        self.api.droplets[778] = self.api.droplets[777] | {'id': 778}
        with self.assertRaises(UnresolvedLaunch):
            self.provider.list_instances()
        self.api.droplets.pop(778)
        self.api.droplets[777]['tags'] = [self.provider.driver.pool_tag]
        with self.assertRaises(UnresolvedLaunch):
            self.provider.list_instances()
        self.api.pages = Mock(side_effect=PermissionError())
        with self.assertRaises(PermissionError):
            self.provider.list_instances()

    def test_delete_refuses_external_volumes(self):
        name, _ = self.complete(self.provider.create_instance)
        self.api.droplets[777]['volume_ids'] = ['shared-volume']
        _, result = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertFalse(result['success'])
        self.assertFalse(any(m == 'DELETE' for m, _, _ in self.api.calls))

    def test_quota_response_is_not_retried_or_downgraded(self):
        original = self.api.request
        def reject(method, path, **kwargs):
            if method == 'POST' and path == '/v2/droplets':
                self.api.calls.append((method, path, kwargs['body']))
                raise DigitalOceanAPIError(422)
            return original(method, path, **kwargs)
        self.api.request = reject
        _, result = self.complete(self.provider.create_instance)
        self.assertFalse(result['success'])
        with self.assertRaises(UnresolvedLaunch):
            self.provider.create_instance()
        self.assertEqual(sum(m == 'POST' and p == '/v2/droplets' for m, p, _ in self.api.calls), 1)

    def test_settings_and_preflight_reject_unsafe_options(self):
        for kwargs in ({'capacity_type': 'on-demand'},
                       {'size': 'gpu-mi300x1-192gb'}, {'image_id': 'latest'}, {'region': 'nyc'},
                       {'public_networking': 'false'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                settings(**kwargs)
        for field, value in [('regions', ['nyc3']), ('available', False)]:
            old = self.api.size[field]
            self.api.size[field] = value
            with self.assertRaises(ValueError):
                self.provider.driver.validate()
            self.api.size[field] = old
        self.api.firewall['inbound_rules'] = [{'protocol': 'tcp'}]
        with self.assertRaises(ValueError):
            self.provider.driver.validate()

    def test_team_identity_must_match(self):
        self.api.account['team'] = {'uuid': 'differentteam123'}
        with self.assertRaises(ValueError):
            self.provider.list_instances()

    def test_polling_has_a_deadline(self):
        clock = Mock(side_effect=[0, 901])
        driver = DigitalOceanDriver(settings(), 'gpu-do', self.api, clock=clock, sleep=Mock())
        with self.assertRaises(TimeoutError):
            driver._wait(lambda: False)


class HTTPTests(unittest.TestCase):
    def response(self, body, status=200):
        return Mock(status_code=status, json=Mock(return_value=body))

    def test_pagination_and_token_stays_at_expected_origin(self):
        session = Mock()
        session.request.side_effect = [self.response({'droplets': [{'id': 1}], 'links': {'pages': {
            'next': 'https://api.digitalocean.com/v2/droplets?page=2'}}}), self.response({'droplets': [{'id': 2}]})]
        api = DigitalOceanAPI('fake-token', session)
        self.assertEqual(list(api.pages('/v2/droplets', 'droplets')), [{'id': 1}, {'id': 2}])
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        session.request.side_effect = None
        session.request.return_value = self.response({'droplets': [], 'links': {'pages': {'next': 'https://other.example/v2/droplets'}}})
        with self.assertRaises(RuntimeError):
            list(api.pages('/v2/droplets', 'droplets'))

    def test_error_responses_and_missing_are_distinct(self):
        session = Mock()
        api = DigitalOceanAPI('fake-token', session)
        for status in (401, 403, 422, 429, 500, 302):
            session.request.return_value = self.response({'message': 'secret text'}, status)
            with self.assertRaises(DigitalOceanAPIError) as error:
                api.request('POST', '/v2/droplets', body={'user_data': 'private'})
            self.assertNotIn('secret', str(error.exception))
        self.assertEqual(session.request.call_count, 6)
        session.request.return_value = self.response({}, 404)
        self.assertIsNone(api.request('GET', '/v2/droplets/1', missing_ok=True))
        with self.assertRaises(DigitalOceanAPIError):
            api.request('GET', '/v2/droplets')

    def test_second_page_failure_propagates(self):
        session = Mock()
        session.request.side_effect = [self.response({'droplets': [{'id': 1}], 'links': {'pages': {
            'next': 'https://api.digitalocean.com/v2/droplets?page=2'}}}), self.response({}, 500)]
        with self.assertRaises(DigitalOceanAPIError):
            list(DigitalOceanAPI('fake', session).pages('/v2/droplets', 'droplets'))
