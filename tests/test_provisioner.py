from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import k8s
from provisioner import Pool, build_pools, reconcile_pools


def node(name='emergency-1', ready=True, role='emergency', labels=None, cordoned=False):
    return NS(metadata=NS(name=name, uid=name, creation_timestamp=datetime.now(timezone.utc),
                         labels=labels if labels is not None else {
                             k8s.INSTANCE_IDENTIFIER_LABEL: name, k8s.INSTANCE_PROVIDER_LABEL: 'fake',
                             k8s.NODE_ROLE_LABEL: role, 'smg-cloud-provisioner': 'true'}),
              spec=NS(unschedulable=cordoned),
              status=NS(conditions=[NS(type='Ready', status='True' if ready else 'False')]))


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.provider = Mock(spec=['PROVIDER_NAME', 'NAME_PREFIX', 'list_instances', 'create_instance', 'delete_instance'], PROVIDER_NAME='fake', NAME_PREFIX='emergency')
        self.provider.list_instances.return_value = []
        self.provider.create_instance.return_value = 'emergency-1'
        self.pool = Pool('emergency', self.provider, 'emergency', 1,
                         trigger_delay=10, recovery_delay=20, provisioning_timeout=30,
                         clock=lambda: self.now)
        self.workers = patch('k8s.available_worker_count', return_value=0).start()
        self.nodes = patch('k8s.pool_nodes', return_value=[]).start()
        self.drain = patch('k8s.drain_node', return_value=True).start()
        self.deregister = patch('k8s.deregister_k8s_node').start()
        self.addCleanup(patch.stopall)

    def tick(self, now):
        self.now = now
        self.pool.reconcile()

    def test_launch_delay_and_pending_launch_reservation(self):
        self.tick(0)
        self.tick(9)
        self.provider.create_instance.assert_not_called()
        self.tick(10)
        self.tick(100)
        self.provider.create_instance.assert_called_once()

    def test_existing_instance_prevents_launch_after_restart(self):
        self.provider.list_instances.return_value = ['emergency-1']
        self.nodes.return_value = [node()]
        self.tick(0)
        self.tick(100)
        self.provider.create_instance.assert_not_called()
        self.provider.delete_instance.assert_not_called()

    def test_flapping_resets_outage(self):
        self.tick(0)
        self.workers.return_value = 1
        self.tick(9)
        self.workers.return_value = 0
        self.tick(10)
        self.tick(19)
        self.provider.create_instance.assert_not_called()
        self.tick(20)
        self.provider.create_instance.assert_called_once()

    def test_recovery_flapping_and_drain_wait(self):
        self.provider.list_instances.return_value = ['emergency-1']
        self.nodes.return_value = [node()]
        self.workers.return_value = 1
        self.tick(0)
        self.workers.return_value = 0
        self.tick(19)
        self.workers.return_value = 1
        self.tick(20)
        self.tick(39)
        self.drain.assert_not_called()
        self.drain.return_value = False
        self.tick(40)
        self.provider.delete_instance.assert_not_called()
        self.deregister.assert_not_called()
        self.drain.return_value = True
        self.tick(41)
        self.provider.delete_instance.assert_called_once()
        self.deregister.assert_called_once_with('emergency-1')

    def test_terminated_spot_stale_node_is_cleaned_before_replacement(self):
        self.nodes.return_value = [node(ready=False)]
        self.tick(0)
        self.tick(29)
        self.deregister.assert_not_called()
        self.tick(30)
        self.deregister.assert_called_once_with('emergency-1')
        self.provider.create_instance.assert_not_called()
        callback = self.provider.delete_instance.call_args.kwargs['callback']
        callback(success=True, instance_name='emergency-1', error=None)
        self.nodes.return_value = []
        self.tick(31)
        self.provider.create_instance.assert_called_once()

    def test_ready_node_missing_from_inventory_is_not_removed(self):
        self.nodes.return_value = [node(ready=True)]
        self.tick(0)
        self.tick(300)
        self.deregister.assert_not_called()
        self.provider.create_instance.assert_not_called()

    def test_timeout_removes_unjoined_instance(self):
        self.provider.list_instances.return_value = ['emergency-1']
        self.tick(0)
        self.tick(29)
        self.provider.delete_instance.assert_not_called()
        self.tick(30)
        self.provider.delete_instance.assert_called_once()
        self.provider.create_instance.assert_not_called()

    def test_dry_run_blocks_all_mutations(self):
        self.pool.dry_run = True
        self.tick(0)
        self.tick(10)
        self.provider.create_instance.assert_not_called()
        self.provider.list_instances.return_value = ['emergency-1']
        self.nodes.return_value = [node()]
        self.workers.return_value = 1
        self.tick(20)
        self.tick(40)
        self.drain.assert_not_called()
        self.deregister.assert_not_called()
        self.provider.delete_instance.assert_not_called()

    def test_pool_failure_does_not_skip_other_pool(self):
        broken, working = Mock(), Mock()
        broken.reconcile.side_effect = RuntimeError('unavailable')
        with self.assertLogs('provisioner', level='ERROR'):
            reconcile_pools([broken, working])
        working.reconcile.assert_called_once()

    def test_gpu_cap_and_pool_isolation(self):
        self.pool.name = 'gpu'
        self.pool.role = 'gpu'
        self.provider.list_instances.return_value = ['emergency-1']
        self.nodes.return_value = [node(name='different-1', role='gpu')]
        with patch('k8s.get_current_gpu_pod_count', return_value=10):
            self.tick(0)
        self.provider.create_instance.assert_not_called()

    def test_ec2_only_does_not_construct_gce(self):
        with patch.dict('os.environ', {'GPU_ENABLED': 'false', 'EMERGENCY_ENABLED': 'true',
                        'EMERGENCY_PROVIDER': 'ec2', 'EMERGENCY_STARTUP_SCRIPT': 'script'}, clear=True), \
             patch('ec2.EC2Provider', return_value=self.provider), \
             patch('gce.GCEProvider', side_effect=AssertionError('GCE must stay inactive')):
            self.assertEqual(len(build_pools()), 1)


class WorkerTests(unittest.TestCase):
    def test_availability_filters(self):
        nodes = [node(labels={}), node(labels={}, ready=False), node(labels={}, cordoned=True),
                 node(labels={'node-role.kubernetes.io/control-plane': ''}),
                 node(labels={'node-role.kubernetes.io/master': ''}), node()]
        with patch.object(k8s, 'ALL_NODES', nodes):
            self.assertEqual(k8s.available_worker_count(), 1)

    def test_node_refresh_removes_stale_nodes(self):
        api = Mock()
        api.list_node.return_value.items = [node()]
        with patch.object(k8s, 'v1', api):
            k8s.update_nodes_list()
            self.assertEqual(len(k8s.pool_nodes('emergency')), 1)
            api.list_node.return_value.items = []
            k8s.update_nodes_list()
            self.assertEqual(k8s.pool_nodes('emergency'), [])
