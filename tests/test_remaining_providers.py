import base64
from dataclasses import replace
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import Mock

from lifecycle import UnresolvedLaunch
from managed_provider import InstanceState
from nebius_provider import NebiusDriver, NebiusSettings
from ovhcloud_provider import OVHDriver, OVHSettings
from scaleway_provider import ScalewayDriver, ScalewaySettings, ScalewayAPI
from provider_factory import factory

NAME = 'smg-test-0123456789abcdef'
PREFIX = 'smg-test'
UUID = '00000000-0000-4000-8000-000000000001'


class RemainingTests(unittest.TestCase):
    def test_registration_limits(self):
        for provider in ('scaleway', 'nebius', 'ovhcloud'):
            self.assertTrue(factory.registration(provider).capabilities.durable_resources)
            factory.validate_pool(provider, 8)
            script = factory.registration(provider).gpu_script('gpu-test')
            self.assertIn('registries.yaml', script)
            self.assertIn('nvidia-smi', script)

    def test_explicit_capacity_only(self):
        for settings in (ScalewaySettings(UUID, 'fr-par-1', 'GPU', UUID, UUID, 'on-demand'),
                         NebiusSettings('project', 'subnet', 'image', 'platform', 'preset', ['sg'], 'on-demand'),
                         OVHSettings('project', 'region', 'https://identity.example/v3', 'flavor', 'image', 'net', 'subnet', ['sg'], 'key', 'on-demand')):
            with self.assertRaises(ValueError):
                replace(settings, capacity_type='spot')

    def nebius(self):
        import nebius.api.nebius.compute.v1 as n
        import nebius.api.nebius.vpc.v1 as v
        d = NebiusDriver.__new__(NebiusDriver)
        d.n, d.v, d.prefix = n, v, PREFIX
        d.settings = NebiusSettings('project', 'subnet', 'image', 'platform', 'preset', ['sg'], 'on-demand')
        d.instances, d.disks = Mock(), Mock()
        return d

    def test_nebius_native_purchasing_fields(self):
        d = self.nebius()
        d.settings = replace(d.settings, purchase_spec={'preemptible': {'on_preemption': 1}})
        self.assertEqual(int(d._purchase()['preemptible'].on_preemption), 1)
        d.settings = replace(d.settings, purchase_spec={'spot_pricing_policy': {'id': 'policy-user'}})
        self.assertEqual(d._purchase()['spot_pricing_policy'].id, 'policy-user')
        with self.assertRaises(ValueError):
            replace(d.settings, purchase_spec={'preemptible': {}, 'on_demand': {}})
        d.settings = replace(d.settings, purchase_spec={'preemptible': {'invalid_field': True}})
        with self.assertRaises(TypeError):
            d._purchase()

    def test_nebius_real_sdk_messages_and_checkpoint_order(self):
        d = self.nebius()
        recorded, calls = {}, []
        def call(method, request):
            calls.append(request)
            key = 'disk' if len(calls) == 1 else 'instance'
            if key == 'instance':
                self.assertEqual(recorded['disk_id'], 'disk-id')
            op = Mock(id=key + '-operation', resource_id=key + '-id')
            op.successful.return_value = True
            return op
        d._call = call
        d.create(NAME, d.plan(NAME), '#!/bin/sh\necho test', recorded.update)
        self.assertEqual(recorded, {'disk_operation_id': 'disk-operation', 'disk_id': 'disk-id',
                                    'instance_operation_id': 'instance-operation', 'instance_id': 'instance-id'})
        self.assertEqual(calls[0].spec.source_image_id, 'image')
        self.assertEqual(calls[1].spec.boot_disk.existing_disk.id, 'disk-id')
        self.assertEqual(calls[1].spec.network_interfaces[0].security_groups[0].id, 'sg')
        self.assertEqual(calls[1].metadata.labels['smgp-instance'], NAME)

    def test_nebius_failed_operation_tracks_identity(self):
        d = self.nebius()
        op = Mock(id='operation', resource_id='disk-id')
        op.successful.return_value = False
        d._call = Mock(return_value=op)
        recorded = {}
        with self.assertRaises(RuntimeError):
            d.create(NAME, {}, 'script', recorded.update)
        self.assertEqual(recorded['disk_id'], 'disk-id')
        self.assertEqual(d._call.call_count, 1)

    def test_nebius_pagination_and_duplicate_recovery(self):
        d = self.nebius()
        d._call = Mock(side_effect=[Obj(items=[1], next_page_token='next'), Obj(items=[2], next_page_token='')])
        self.assertEqual(d._list(d.instances, d.n.ListInstancesRequest), [1, 2])
        from nebius.api.nebius.common.v1 import ResourceMetadata
        obj = d.n.Instance(metadata=ResourceMetadata(id='vm', parent_id='project', name=NAME, labels=d._labels(NAME)),
                           status=d.n.InstanceStatus(state=4))
        d._inventory = Mock(return_value=([obj, obj], []))
        with self.assertRaises(UnresolvedLaunch):
            d.discover_resources(NAME, {})

    def test_nebius_refuses_foreign_disk(self):
        d = self.nebius()
        resources = d.plan(NAME) | {'disk_id': 'disk'}
        d.discover_resources = Mock(return_value=resources)
        from nebius.api.nebius.common.v1 import ResourceMetadata
        d._get = Mock(return_value=d.n.Disk(metadata=ResourceMetadata(id='disk', parent_id='other')))
        with self.assertRaises(ValueError):
            d.delete(NAME, resources)
        d.disks.delete.assert_not_called()

    def ovh(self):
        c = Mock()
        c.session.get_project_id.return_value = 'project'
        settings = OVHSettings('project', 'region', 'https://identity.example/v3', 'flavor', 'image', 'net', 'subnet', ['sg'], 'key', 'on-demand')
        return OVHDriver(settings, PREFIX, c), c

    def test_ovh_sdk_wire_fields_and_checkpoints(self):
        from openstack.compute.v2.server import Server
        from openstack.block_storage.v3.volume import Volume
        d, c = self.ovh()
        c.block_storage.create_volume.return_value = Obj(id='volume')
        c.network.create_port.return_value = Obj(id='port')
        c.compute.create_server.return_value = Obj(id='server')
        recorded = {}
        c.network.create_port.side_effect = lambda **kw: (self.assertEqual(recorded['volume_id'], 'volume') or Obj(id='port'))
        d.create(NAME, {}, 'script', recorded.update)
        params = c.compute.create_server.call_args.kwargs
        server = Server(**params)
        self.assertEqual(server.block_device_mapping[0]['uuid'], 'volume')
        self.assertEqual(Server.block_device_mapping.name, 'block_device_mapping_v2')
        self.assertEqual(base64.b64decode(server.user_data), b'script')
        self.assertEqual(Volume.project_id.name, 'os-vol-tenant-attr:tenant_id')
        self.assertEqual(recorded, {'volume_id': 'volume', 'port_id': 'port', 'server_id': 'server'})

    def test_ovh_cleanup_guards_external_attachments(self):
        d, c = self.ovh()
        resources = d.plan(NAME) | {'server_id': 'vm', 'volume_id': 'disk', 'port_id': 'port'}
        d.discover_resources = Mock(return_value=resources)
        c.compute.get_server.return_value = Obj(project_id='project', metadata=d._metadata(NAME), attached_volumes=[{'id': 'foreign'}])
        with self.assertRaises(ValueError):
            d.delete(NAME, resources)
        c.compute.delete_server.assert_not_called()
        c.block_storage.delete_volume.assert_not_called()

    def test_ovh_auth_failure_not_empty_inventory(self):
        d, c = self.ovh()
        c.compute.servers.side_effect = PermissionError()
        with self.assertRaises(PermissionError):
            d.inventory()

    def scaleway(self):
        api = Mock()
        settings = ScalewaySettings(UUID, 'fr-par-1', 'GPU', UUID, UUID, 'on-demand')
        return ScalewayDriver(settings, PREFIX, api), api

    def test_scaleway_userdata_before_poweron(self):
        d, a = self.scaleway()
        a.request.side_effect = [ {'ip': {'id': 'ip'}},
            {'server': {'id': 'server', 'volumes': {'0': {'id': 'disk'}}}}, {}, {}, {'server': {'state': 'running'}}]
        tracked = {}
        d.create(NAME, {}, 'script', tracked.update)
        self.assertEqual(tracked, {'ip_id': 'ip', 'server_id': 'server', 'volume_id': 'disk'})
        calls = a.request.call_args_list
        self.assertEqual(calls[2].args, ('PATCH', 'servers/server/user_data/cloud-init'))
        self.assertEqual(calls[3].kwargs['json'], {'action': 'poweron'})
        self.assertEqual(calls[1].kwargs['json']['public_ips'], ['ip'])

    def test_scaleway_failed_userdata_never_poweron(self):
        d, a = self.scaleway()
        a.request.side_effect = [{'ip': {'id': 'ip'}}, {'server': {'id': 'vm', 'volumes': {'0': {'id': 'disk'}}}}, TimeoutError()]
        tracked = {}
        with self.assertRaises(TimeoutError):
            d.create(NAME, {}, 'script', tracked.update)
        self.assertEqual(tracked['volume_id'], 'disk')
        self.assertEqual(a.request.call_count, 3)

    def test_scaleway_foreign_attachment_blocks_delete(self):
        d, a = self.scaleway()
        resources = d.plan(NAME) | {'server_id': 'vm', 'volume_id': 'disk'}
        d.discover_resources = Mock(return_value=resources)
        a.request.return_value = {'server': {'project': UUID, 'tags': d._tags(NAME), 'volumes': {'0': {'id': 'foreign'}}}}
        with self.assertRaises(ValueError):
            d.delete(NAME, resources)
        self.assertEqual(a.request.call_count, 1)

    def test_scaleway_transport_no_redirect_or_error_body(self):
        a = ScalewayAPI('secret', 'fr-par-1')
        a.session = Mock()
        a.session.request.return_value = Obj(status_code=403, text='sensitive detail')
        with self.assertRaisesRegex(RuntimeError, '^Scaleway request failed \\(HTTP 403\\)$'):
            a.request('GET', 'servers')
        self.assertFalse(a.session.request.call_args.kwargs['allow_redirects'])
