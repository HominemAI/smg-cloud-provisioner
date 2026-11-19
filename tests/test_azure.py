import base64
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from azure_provider import AzureSettings, AzureDriver
from managed_provider import InstanceState


def settings(**overrides):
    values = dict(subscription_id='test-subscription', resource_group='gpu-rg', location='westus',
        subnet_id='/subscriptions/test-subscription/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/vnet/subnets/gpu',
        nsg_id='/subscriptions/test-subscription/resourceGroups/network/providers/Microsoft.Network/networkSecurityGroups/gpu',
        vm_size='Standard_TestGPU', image_id='/subscriptions/test-subscription/resourceGroups/images/providers/Microsoft.Compute/images/gpu-v1',
        ssh_public_key='ssh-ed25519 TEST', capacity_type='spot')
    return AzureSettings(**(values | overrides))


class AzureTests(unittest.TestCase):
    def setUp(self):
        self.compute, self.network = Mock(), Mock()
        self.driver = AzureDriver(settings(), 'gpu-azure', self.compute, self.network)
        self.name = 'gpu-azure-' + 'a' * 16
        self.resources = self.driver.plan(self.name)
        self.network.network_interfaces.list.return_value = []
        self.network.public_ip_addresses.list.return_value = []
        self.compute.disks.list_by_resource_group.return_value = []
        for kind in ('vm', 'disk', 'nic', 'ip'):
            ops = self.driver._ops(kind)
            ops.get.side_effect = ResourceNotFoundError()
            ops.begin_create_or_update.return_value.done.return_value = True
            ops.begin_delete.return_value.done.return_value = True

    def sku(self, **changes):
        base = dict(name='Standard_TestGPU', resource_type='virtualMachines', locations=['westus'],
                    capabilities=[NS(name='GPUs', value='1'), NS(name='LowPriorityCapable', value='True')],
                    location_info=[NS(location='westus', zones=['1', '2'])], restrictions=[])
        return NS(**(base | changes))

    def test_gpu_zone_and_subscription_restrictions(self):
        self.compute.resource_skus.list.return_value = iter([self.sku(name='other'), self.sku()])
        self.driver.validate()
        self.driver.settings = replace(self.driver.settings, zone='3')
        self.compute.resource_skus.list.return_value = [self.sku()]
        with self.assertRaisesRegex(ValueError, 'zone'):
            self.driver.validate()
        self.driver.settings = replace(self.driver.settings, zone='1')
        restricted = NS(type='Zone', values=['westus'], restriction_info=NS(locations=['westus'], zones=['1']))
        self.compute.resource_skus.list.return_value = [self.sku(restrictions=[restricted])]
        with self.assertRaisesRegex(ValueError, 'restricted'):
            self.driver.validate()
        self.compute.resource_skus.list.return_value = [self.sku(capabilities=[NS(name='GPUs', value='8')])]
        self.driver.settings = replace(self.driver.settings, capacity_type='on-demand')
        self.driver.validate()

    def test_spot_payload_and_real_sdk_serialization(self):
        self.driver.create(self.name, self.resources, '#!/bin/bash\necho fake')
        params = self.compute.virtual_machines.begin_create_or_update.call_args.args[2]
        # Validate against installed SDK serializers, not only our own fake client.
        client = ComputeManagementClient(Mock(), 'test-subscription')
        wire = client.virtual_machines._serialize.body(params, 'VirtualMachine')
        props = wire['properties']
        self.assertEqual(props['priority'], 'Spot')
        self.assertEqual(props['evictionPolicy'], 'Delete')
        self.assertEqual(props['storageProfile']['osDisk']['deleteOption'], 'Delete')
        self.assertEqual(props['networkProfile']['networkInterfaces'][0]['properties']['deleteOption'], 'Delete')
        self.assertEqual(base64.b64decode(props['osProfile']['customData']), b'#!/bin/bash\necho fake')
        self.assertNotIn('zones', wire)
        self.network.public_ip_addresses.begin_create_or_update.assert_not_called()

    def test_public_ip_nic_serialization_and_on_demand(self):
        self.driver.settings = replace(self.driver.settings, public_ip=True, zone='1', capacity_type='on-demand')
        self.driver.create(self.name, self.driver.plan(self.name), 'fake')
        client = NetworkManagementClient(Mock(), 'test-subscription')
        nic = self.network.network_interfaces.begin_create_or_update.call_args.args[2]
        wire = client._serialize.body(nic, 'NetworkInterface')
        ip = wire['properties']['ipConfigurations'][0]['properties']['publicIPAddress']
        self.assertEqual(ip['properties']['deleteOption'], 'Delete')
        params = self.compute.virtual_machines.begin_create_or_update.call_args.args[2]
        self.assertEqual(params['priority'], 'Regular')
        self.assertNotIn('billing_profile', params)
        self.assertNotIn('eviction_policy', params)

    def test_inventory_ownership_and_pagination_iterator(self):
        good = NS(name=self.name, tags=self.driver._tags(self.name), provisioning_state='Succeeded')
        other = NS(name=self.name + '-other', tags={'smgp-pool': 'someone-else'}, provisioning_state='Succeeded')
        self.compute.virtual_machines.list.return_value = iter([other, good])
        self.assertEqual(self.driver.inventory(), {self.name: InstanceState.READY})
        def broken_pages():
            yield good
            raise PermissionError('second page failed')
        self.compute.virtual_machines.list.return_value = broken_pages()
        with self.assertRaises(PermissionError):
            self.driver.inventory()

    def test_cleanup_is_idempotent_and_rejects_other_pool(self):
        self.driver.delete(self.name, self.resources)
        self.compute.virtual_machines.begin_delete.assert_not_called()
        with self.assertRaises(ValueError):
            self.driver.delete('another-pool-' + 'a' * 16, self.resources)
        self.compute.virtual_machines.get.side_effect = None
        self.compute.virtual_machines.get.return_value = NS(tags={})
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.driver.delete(self.name, self.resources)
        self.compute.virtual_machines.begin_delete.assert_not_called()

    def test_orphans_detected_after_journal_loss(self):
        self.compute.virtual_machines.list.return_value = []
        self.network.network_interfaces.list.return_value = [NS(tags=self.driver._tags(self.name))]
        self.assertEqual(self.driver.inventory(), {self.name: InstanceState.ORPHANED})
        self.network.network_interfaces.list.return_value = []
        self.compute.disks.list_by_resource_group.return_value = [NS(name=self.name + '-os')]
        self.assertEqual(self.driver.inventory(), {self.name: InstanceState.ORPHANED})

    def test_changed_attachments_block_vm_delete(self):
        self.compute.virtual_machines.get.side_effect = None
        self.compute.virtual_machines.get.return_value = NS(tags=self.driver._tags(self.name),
            storage_profile=NS(data_disks=[NS(name='shared')]),
            network_profile=NS(network_interfaces=[NS(id=self.resources['nic'])]))
        with self.assertRaisesRegex(ValueError, 'attachments'):
            self.driver.delete(self.name, self.resources)
        self.compute.virtual_machines.begin_delete.assert_not_called()

    def test_complete_cleanup_verifies_every_resource_is_gone(self):
        self.driver.settings = replace(self.driver.settings, public_ip=True)
        self.resources = self.driver.plan(self.name)
        vm = NS(tags=self.driver._tags(self.name), storage_profile=NS(data_disks=[],
            os_disk=NS(managed_disk=NS(id=self.resources['disk']))),
            network_profile=NS(network_interfaces=[NS(id=self.resources['nic'])]))
        for kind, resource in [('vm', vm), ('nic', NS(tags=self.driver._tags(self.name))),
                               ('ip', NS(tags=self.driver._tags(self.name))),
                               ('disk', NS(managed_by=None, managed_by_extended=[]))]:
            self.driver._ops(kind).get.side_effect = [resource, ResourceNotFoundError()]
        self.driver.delete(self.name, self.resources)
        for kind, resource_id in self.resources.items():
            self.driver._ops(kind).begin_delete.assert_called_once_with('gpu-rg', resource_id.rsplit('/', 1)[1])

    def test_partial_cleanup_tracks_nic_and_disk_and_refuses_attached_disk(self):
        self.network.network_interfaces.get.side_effect = [NS(tags=self.driver._tags(self.name)), ResourceNotFoundError()]
        self.compute.disks.get.side_effect = [NS(managed_by='/someone/else', managed_by_extended=None)]
        with self.assertRaisesRegex(ValueError, 'attached'):
            self.driver.delete(self.name, self.resources)
        self.network.network_interfaces.begin_delete.assert_called_once_with('gpu-rg', self.name + '-nic')
        self.compute.disks.begin_delete.assert_not_called()

    def test_timeout_never_means_success(self):
        poller = Mock()
        poller.done.return_value = False
        with self.assertRaises(TimeoutError):
            self.driver._wait(poller)

    def test_config_refuses_latest_implicit_market_and_foreign_network(self):
        for changes in ({'capacity_type': ''}, {'image_id': 'publisher:offer:sku:latest'},
                        {'public_ip': 'false'}, {'subnet_id': '/subscriptions/other/subnets/foo'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings(**changes)
