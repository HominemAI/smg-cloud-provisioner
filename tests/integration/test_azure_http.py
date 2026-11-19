import tempfile
import time
import unittest
from pathlib import Path

import requests
from azure.core.credentials import AccessToken
from azure.core.pipeline.policies import BearerTokenCredentialPolicy
from azure.core.pipeline.transport import RequestsTransport
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from azure_provider import AzureProvider, AzureSettings
from lifecycle import UnresolvedLaunch
from azure_simulator import AzureSimulator
from support import JSONServer, completion


class FakeCredential:
    def get_token(self, *args, **kwargs):
        return AccessToken('local-test-token', int(time.time()) + 3600)


class LoopbackAuthentication(BearerTokenCredentialPolicy):
    def on_request(self, request):
        if not request.http_request.url.startswith('http://127.0.0.1:'):
            raise AssertionError('Test authentication may only be used on loopback')
        request.context.options['enforce_https'] = False
        super().on_request(request)


class AzureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = AzureSimulator()
        self.server = JSONServer(self.app)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = str(Path(self.temp.name) / 'state.sqlite')
        self.settings = AzureSettings(subscription_id='test-subscription', resource_group='gpu-rg',
            location='westus', subnet_id='/subscriptions/test-subscription/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/vnet/subnets/gpu',
            nsg_id='/subscriptions/test-subscription/resourceGroups/network/providers/Microsoft.Network/networkSecurityGroups/gpu',
            vm_size='Standard_TestGPU', image_id='/subscriptions/test-subscription/resourceGroups/images/providers/Microsoft.Compute/images/gpu-v1',
            ssh_public_key='ssh-ed25519 TEST', capacity_type='spot', public_ip=True, operation_timeout=5)
        self.provider = self.new_provider()

    def new_provider(self):
        def client(cls):
            session = requests.Session()
            session.trust_env = False
            credential = FakeCredential()
            result = cls(credential, 'test-subscription', base_url=self.server.url,
                authentication_policy=LoopbackAuthentication(credential, 'local/.default'),
                transport=RequestsTransport(session=session), polling_interval=0,
                retry_total=0, connection_timeout=2, read_timeout=2)
            self.addCleanup(result.close)
            return result
        return AzureProvider(self.settings, self.ledger, compute=client(ComputeManagementClient),
            network=client(NetworkManagementClient), name_prefix='gpu-azure', startup_script='#!/bin/sh\necho fake')

    def test_actual_sdks_serialize_poll_inventory_and_delete(self):
        name, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'], str(result['error']))
        self.assertEqual(self.provider.list_instances(), [name])
        self.assertEqual(len(self.app.resources), 4)
        vm = next(body for method, path, body in self.app.requests if method == 'PUT' and '/virtualMachines/' in path)
        self.assertEqual(vm['properties']['priority'], 'Spot')
        self.assertEqual(vm['properties']['evictionPolicy'], 'Delete')
        self.assertTrue(all(op['polls'] >= 2 for op in self.app.operations.values()))
        _, result = completion(self, lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(result['success'], str(result['error']))
        self.assertEqual(self.app.resources, {})
        self.assertEqual(self.provider.list_instances(), [])

    def test_two_nodes_restart_and_delete_one(self):
        first, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'])
        second, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'])
        restarted = self.new_provider()
        self.assertEqual(set(restarted.list_instances()), {first, second})
        _, result = completion(self, lambda **kw: restarted.delete_instance(first, **kw))
        self.assertTrue(result['success'])
        self.assertEqual(restarted.list_instances(), [second])
        _, result = completion(self, lambda **kw: restarted.delete_instance(second, **kw))
        self.assertTrue(result['success'])
        self.assertEqual(self.app.resources, {})

    def test_lost_response_recovers_after_restart_without_second_vm(self):
        self.app.lose_vm_reply = True
        name, result = completion(self, self.provider.create_instance)
        self.assertFalse(result['success'])
        restarted = self.new_provider()
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(restarted.list_instances(), [name])
        self.assertEqual(sum(m == 'PUT' and '/virtualMachines/' in p for m, p, _ in self.app.requests), 1)

    def test_cleanup_conflict_retries_after_restart(self):
        name, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'])
        self.app.fail_nic_delete = True
        _, result = completion(self, lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertFalse(result['success'])
        self.app.fail_nic_delete = False
        restarted = self.new_provider()
        self.assertEqual(restarted.list_instances(), [name])
        _, result = completion(self, lambda **kw: restarted.delete_instance(name, **kw))
        self.assertTrue(result['success'], str(result['error']))
        self.assertEqual(self.app.resources, {})

    def test_eviction_cleans_auxiliaries_and_inventory_outage_blocks_launch(self):
        name, result = completion(self, self.provider.create_instance)
        self.assertTrue(result['success'])
        self.app.fail_second_page = True
        with self.assertRaises(Exception):
            self.provider.list_instances()
        self.app.fail_second_page = False
        vm_id = self.provider.driver.plan(name)['vm']
        del self.app.resources[vm_id]
        self.assertEqual(self.provider.list_instances(), [name])
        _, result = completion(self, lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(result['success'])
        self.assertEqual(self.app.resources, {})
