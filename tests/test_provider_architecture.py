import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from bootstrap import gpu_bootstrap
from cloud_provider import CloudProvider
from provider_factory import ProviderFactory, ProviderCapabilities
from provisioner import build_pools, Pool


class ArchitectureTests(unittest.TestCase):
    def test_new_provider_registration_requires_no_scaler_changes(self):
        factory = ProviderFactory()
        provider = Mock()
        factory.register('future-cloud', lambda **kw: provider,
                         ProviderCapabilities(frozenset({'spot'}), max_pool_nodes=1))
        self.assertIs(factory.create_gpu('future-cloud', 'gpu-future'), provider)
        with self.assertRaises(ValueError):
            factory.validate_pool('future-cloud', 2)
        with self.assertRaises(ValueError):
            factory.register('future-cloud', Mock())

    def test_json_pools_override_legacy_and_remain_opt_in(self):
        env = {'GPU_ENABLED': 'true', 'GPU_EC2_ENABLED': 'true', 'GPU_POOLS_JSON': '[]'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(build_pools(), [])
        env['GPU_POOLS_JSON'] = json.dumps([{'provider': 'not-yet-built', 'enabled': False}])
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(build_pools(), [])

    def test_azure_config_uses_registry_and_common_bootstrap(self):
        with tempfile.TemporaryDirectory() as temp:
            spec = {'name': 'gpu-azure', 'provider': 'azure', 'role': 'gpu-azure', 'enabled': True,
                    'options': {'settings': {'placeholder': True}, 'ledger_path': temp + '/state.sqlite'}}
            fake = Mock(spec=['PROVIDER_NAME', 'NAME_PREFIX'], PROVIDER_NAME='azure', NAME_PREFIX='gpu-azure')
            with patch.dict(os.environ, {'GPU_POOLS_JSON': json.dumps([spec]), 'DRY_RUN': 'true'}, clear=True), \
                 patch('azure_provider.AzureProvider', return_value=fake) as builder:
                pool, = build_pools()
            self.assertTrue(pool.dry_run)
            self.assertEqual(pool.max_nodes, 1)
            self.assertIn('--node-taint nvidia.com/gpu=gpu-azure:NoSchedule', builder.call_args.kwargs['startup_script'])

    def test_policy_depends_on_scheduler_interface(self):
        provider = Mock(spec=['PROVIDER_NAME', 'NAME_PREFIX', 'list_instances', 'create_instance'],
                        PROVIDER_NAME='fake', NAME_PREFIX='gpu')
        provider.list_instances.return_value = []
        scheduler = Mock()
        scheduler.pool_nodes.return_value = []
        scheduler.gpu_demand.return_value = 1
        pool = Pool('gpu-future', provider, 'gpu-future', 1, scheduler=scheduler)
        pool.reconcile()
        scheduler.gpu_demand.assert_called_once_with('gpu-future')
        provider.create_instance.assert_called_once()

    def test_common_bootstrap_valid_for_each_hook(self):
        for hook, role in [('gpu-ec2-prepare.sh', 'gpu-aws'), ('gpu-azure-prepare.sh', 'gpu-azure'),
                           ('gpu-digitalocean-prepare.sh', 'gpu-do')]:
            script = gpu_bootstrap(hook, role)
            required, _ = CloudProvider.parse_startup_script(script)
            with patch.dict(os.environ, {key: 'testvalue' for key in required}, clear=True):
                rendered = CloudProvider.substitute_startup_script_vars(script,
                    {'INSTANCE_NAME': 'gpu-test', 'INSTANCE_PROVIDER': 'test'})
            subprocess.run(['bash', '-n'], input=rendered, text=True, check=True)
            self.assertLess(rendered.index('chmod 0600'), rendered.index('https://get.k3s.io'))
            self.assertNotIn('# PROVIDER_GPU_PREPARE', rendered)
        azure_hook = Path('scripts/gpu-azure-prepare.sh').read_text()
        self.assertNotIn('mkfs', azure_hook)
        self.assertNotIn('wipefs', azure_hook)

    def test_digitalocean_uses_registry_without_scaler_branch(self):
        spec = {'name': 'gpu-do', 'provider': 'digitalocean', 'role': 'gpu-do', 'enabled': True,
                'options': {'settings': {}, 'ledger_path': '/unused/example.sqlite'}}
        fake = Mock(spec=['PROVIDER_NAME', 'NAME_PREFIX'], PROVIDER_NAME='digitalocean', NAME_PREFIX='gpu-do')
        with patch.dict(os.environ, {'GPU_POOLS_JSON': json.dumps([spec]), 'DRY_RUN': 'true'}, clear=True), \
             patch('digitalocean_provider.DigitalOceanProvider', return_value=fake) as builder:
            pool, = build_pools()
        self.assertEqual(pool.role, 'gpu-do')
        self.assertTrue(pool.dry_run)
        self.assertIn('nvidia.com/gpu=gpu-do:NoSchedule', builder.call_args.kwargs['startup_script'])

    def test_invalid_json_limits_fail_before_building_cloud(self):
        base = {'name': 'gpu', 'provider': 'azure', 'role': 'gpu-azure', 'enabled': True, 'options': {}}
        for change in ({'enabled': 'true'}, {'max_nodes': -1}, {'name': 'emergency'}, {'typo': True}):
            with patch.dict(os.environ, {'GPU_POOLS_JSON': json.dumps([base | change])}, clear=True), \
                 patch('azure_provider.AzureProvider') as builder, self.assertRaises(ValueError):
                build_pools()
            builder.assert_not_called()
