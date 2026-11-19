import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState
from placement import PlacementProvider
from pool_configuration import gpu_provider


class PoolPlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'ledger.db')

    def complete(self, method):
        event, result = threading.Event(), {}
        def callback(**kw):
            result.update(kw)
            event.set()
        name = method(callback=callback)
        self.assertTrue(event.wait(3))
        return name, result

    def test_multiple_nodes_restart_and_independent_delete(self):
        cloud = {}
        driver = Mock(scope='cloud/pool')
        driver.inventory.side_effect = lambda: dict(cloud)
        driver.plan.side_effect = lambda name: {'name': name}
        driver.discover_resources.side_effect = lambda name, resources: resources
        driver.create.side_effect = lambda name, *args: cloud.update({name: InstanceState.READY})
        driver.delete.side_effect = lambda name, resources: cloud.pop(name)
        def provider():
            return ManagedProvider(driver, LifecycleStore(self.path), provider_name='test', name_prefix='pool', startup_script='echo test')
        p = provider()
        first, _ = self.complete(p.create_instance)
        second, _ = self.complete(p.create_instance)
        restarted = provider()
        self.assertEqual(set(restarted.list_instances()), {first, second})
        _, result = self.complete(lambda callback: restarted.delete_instance(first, callback))
        self.assertTrue(result['success'])
        self.assertEqual(restarted.list_instances(), [second])

    def test_upgrade_preserves_old_records(self):
        store = LifecycleStore(self.path)
        store.reserve('pool', 'existing', {})
        store.update('pool', 'existing', 'ready')
        with store.connect() as db:
            db.execute('DROP INDEX one_pending_launch')
            db.execute("CREATE UNIQUE INDEX one_active_launch ON launches(scope) WHERE phase != 'deleted'")
        upgraded = LifecycleStore(self.path)
        upgraded.reserve('pool', 'new', {})
        self.assertEqual({r['name'] for r in upgraded.records('pool')}, {'existing', 'new'})

    def test_definitive_rejection_advances_without_ambiguity(self):
        first, second = Mock(NAME_PREFIX='pool-a'), Mock(NAME_PREFIX='pool-b')
        for p in (first, second):
            p.list_instances.return_value = []
        error = RuntimeError('confirmed rejection')
        error.launch_rejected = True
        first.create_instance.side_effect = lambda callback: callback(success=False, instance_name='pool-a-no-vm', error=error)
        second.create_instance.side_effect = lambda callback: (callback(success=True, instance_name='pool-b-vm', error=None) or 'pool-b-vm')
        p = PlacementProvider([first, second], 'pool', self.path)
        p.create_instance()
        self.assertEqual(p.create_instance(), 'pool-b-vm')

    def test_atomic_pending_limit_allows_ready_instances(self):
        store = LifecycleStore(self.path)
        store.reserve('pool', 'first', {})
        store.update('pool', 'first', 'ready')
        store.reserve('pool', 'second', {})
        with self.assertRaises(UnresolvedLaunch):
            LifecycleStore(self.path).reserve('pool', 'third', {})
        self.assertEqual(len(store.records('pool')), 2)

    def test_ordered_preflight_failure_and_success_reset(self):
        first, second = Mock(NAME_PREFIX='pool-a'), Mock(NAME_PREFIX='pool-b')
        for p in (first, second):
            p.list_instances.return_value = []
            p.store.records.return_value = []
        first.create_instance.side_effect = ValueError('bad spec')
        second.create_instance.side_effect = lambda callback: (callback(success=True, instance_name='pool-b-vm', error=None) or 'pool-b-vm')
        p = PlacementProvider([first, second], 'pool', self.path)
        with self.assertRaises(ValueError):
            p.create_instance()
        self.assertEqual(p.create_instance(), 'pool-b-vm')
        self.assertEqual(p.index, 0)

    def test_lost_response_blocks_fallback_across_restart(self):
        first, second = Mock(NAME_PREFIX='pool-a'), Mock(NAME_PREFIX='pool-b')
        for p in (first, second):
            p.list_instances.return_value = []
        first.create_instance.side_effect = lambda callback: (callback(success=False, instance_name='pool-a-vm', error=TimeoutError()) or 'pool-a-vm')
        p = PlacementProvider([first, second], 'pool', self.path)
        p.create_instance()
        restarted = PlacementProvider([first, second], 'pool', self.path)
        with self.assertRaises(UnresolvedLaunch):
            restarted.create_instance()
        second.create_instance.assert_not_called()
        first.list_instances.return_value = ['pool-a-vm']
        self.assertEqual(restarted.list_instances(), ['pool-a-vm'])
        self.assertEqual(restarted.store.records(restarted.scope), [])

    def test_candidate_ids_stable_when_reordered(self):
        candidates = [{'id': 'west', 'provider': 'gce', 'options': {'template': 'west-template'}},
                      {'id': 'east', 'provider': 'gce', 'options': {'template': 'east-template'}}]
        spec = {'name': 'gpu', 'role': 'gpu', 'max_nodes': 4, 'placement_ledger_path': self.path,
                'candidates': candidates}
        with patch('pool_configuration.factory.create_gpu', side_effect=lambda *args, **kw: Mock(NAME_PREFIX=kw['name_prefix'])):
            p = gpu_provider(spec)
            q = gpu_provider(spec | {'candidates': list(reversed(candidates))})
        self.assertEqual([x.NAME_PREFIX for x in p.providers], ['gpu-west', 'gpu-east'])
        self.assertEqual([x.NAME_PREFIX for x in q.providers], ['gpu-east', 'gpu-west'])

    def test_pending_nodes_count_toward_demand(self):
        from scaling_policy import GpuPolicy
        pool = Mock(max_nodes=8, min_nodes=0, role='gpu', creating=False, launch_time=None,
                    deleting=set(), observed={'pending': 0}, provisioning_timeout=1000)
        pool.scheduler.gpu_demand.return_value = 1
        GpuPolicy().reconcile(pool, 1, {'pending'}, {})
        pool.launch.assert_not_called()
