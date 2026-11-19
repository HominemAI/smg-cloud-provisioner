import os
import tempfile
import threading
import unittest
from unittest.mock import Mock

from lifecycle import LifecycleStore, UnresolvedLaunch
from managed_provider import ManagedProvider, InstanceState


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'lifecycle.sqlite')
        self.store = LifecycleStore(self.path)
        self.driver = Mock(scope='fake/test/pool')
        self.driver.inventory.return_value = {}
        self.driver.plan.side_effect = lambda name: {'vm': name, 'disk': name + '-disk'}
        self.driver.discover_resources.side_effect = lambda name, resources: resources
        self.provider = ManagedProvider(self.driver, self.store, provider_name='fake',
                                        name_prefix='gpu', startup_script='echo {{INSTANCE_NAME}}')

    def complete(self, action):
        done, result = threading.Event(), {}
        def callback(**values):
            result.update(values)
            done.set()
        value = action(callback=callback)
        self.assertTrue(done.wait(5), 'callback timed out')
        return value, result

    def test_reservation_precedes_submission_and_tracks_resources(self):
        def create(name, resources, script, resources_changed):
            entry, = self.store.records(self.driver.scope)
            self.assertEqual(entry['phase'], 'submitting')
            self.assertEqual(entry['resources'], resources)
            self.assertNotIn('script', entry)
        self.driver.create.side_effect = create
        name, result = self.complete(self.provider.create_instance)
        self.assertTrue(result['success'])
        self.assertEqual(self.store.records(self.driver.scope)[0]['name'], name)

    def test_uncertain_response_survives_restart_and_blocks_duplicate(self):
        self.driver.create.side_effect = TimeoutError('secret body must not be stored')
        name, result = self.complete(self.provider.create_instance)
        self.assertFalse(result['success'])
        restarted = ManagedProvider(self.driver, LifecycleStore(self.path), provider_name='fake',
                                    name_prefix='gpu', startup_script='echo test')
        with self.assertRaises(UnresolvedLaunch):
            restarted.list_instances()
        with self.assertRaises(UnresolvedLaunch):
            restarted.create_instance()
        with self.assertRaises(UnresolvedLaunch):
            restarted.delete_instance(name)
        self.driver.create.assert_called_once()
        self.assertNotIn('secret body', str(self.store.records(self.driver.scope)))
        self.driver.inventory.return_value = {name: InstanceState.READY}
        self.assertEqual(restarted.list_instances(), [name])

    def test_inventory_failure_never_means_empty(self):
        self.driver.inventory.side_effect = PermissionError()
        with self.assertRaises(PermissionError):
            self.provider.list_instances()
        with self.assertRaises(PermissionError):
            self.provider.create_instance()
        self.driver.create.assert_not_called()

    def test_untracked_vm_blocks_pool(self):
        self.driver.inventory.return_value = {'gpu-existing': InstanceState.READY}
        with self.assertRaises(UnresolvedLaunch):
            self.provider.list_instances()

    def test_evicted_vm_kept_until_auxiliary_cleanup(self):
        name, _ = self.complete(self.provider.create_instance)
        self.assertEqual(self.provider.list_instances(), [name])
        self.driver.delete.assert_not_called()  # Inventory remains read-only (DRY_RUN).
        _, result = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(result['success'])
        self.assertEqual(self.provider.list_instances(), [])
        self.assertEqual(self.store.records(self.driver.scope), [])

    def test_cleanup_failure_can_retry_after_restart(self):
        name, _ = self.complete(self.provider.create_instance)
        self.driver.delete.side_effect = TimeoutError()
        _, result = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertFalse(result['success'])
        self.assertEqual(self.store.records(self.driver.scope)[0]['phase'], 'deleting')
        self.assertEqual(self.provider.list_instances(), [name])
        self.driver.delete.side_effect = None
        _, result = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(result['success'])

    def test_atomic_reservation_prevents_second_controller(self):
        self.store.reserve('scope', 'one', {})
        with self.assertRaises(UnresolvedLaunch):
            LifecycleStore(self.path).reserve('scope', 'two', {})
        self.store.reserve('different-pool', 'two', {})

    def test_server_assigned_ids_are_append_only(self):
        self.store.reserve('scope', 'one', {'name': 'one'})
        self.store.save_resources('scope', 'one', {'droplet_id': 123})
        self.assertEqual(self.store.records('scope')[0]['resources'], {'name': 'one', 'droplet_id': 123})
        with self.assertRaises(ValueError):
            self.store.save_resources('scope', 'one', {'droplet_id': 456})
        self.assertEqual(self.store.records('scope')[0]['resources']['droplet_id'], 123)

    def test_abandoned_partial_launch_requires_explicit_confirmation(self):
        self.driver.create.side_effect = TimeoutError()
        name, _ = self.complete(self.provider.create_instance)
        self.driver.inventory.return_value = {name: InstanceState.ORPHANED}
        with self.assertRaises(UnresolvedLaunch):
            self.provider.list_instances()
        with self.assertRaises(ValueError):
            self.provider.confirm_abandoned(name)
        self.provider.confirm_abandoned(name, no_pending_operation=True)
        self.assertEqual(self.provider.list_instances(), [name])
        _, result = self.complete(lambda **kw: self.provider.delete_instance(name, **kw))
        self.assertTrue(result['success'])

    def test_pending_vm_cannot_be_confirmed_abandoned_or_deleted(self):
        self.driver.create.side_effect = TimeoutError()
        name, _ = self.complete(self.provider.create_instance)
        self.driver.inventory.return_value = {name: InstanceState.PENDING}
        with self.assertRaises(UnresolvedLaunch):
            self.provider.confirm_abandoned(name, no_pending_operation=True)
        with self.assertRaises(UnresolvedLaunch):
            self.provider.delete_instance(name)

    def test_preflight_failure_does_not_reserve(self):
        self.driver.validate.side_effect = ValueError('not offered')
        with self.assertRaises(ValueError):
            self.provider.create_instance()
        self.assertEqual(self.store.records(self.driver.scope), [])
