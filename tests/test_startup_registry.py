import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

from cloud_provider import CloudProvider

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'scripts/emergency-startup.sh').read_text()


class RegistryBootstrapTests(unittest.TestCase):
    def test_registry_written_privately_before_k3s_without_logging_token(self):
        block = SCRIPT[SCRIPT.index('# Install registry credentials'):SCRIPT.index('export INSTALL_K3S_VERSION')]
        self.assertLess(SCRIPT.index('GHCR_REGISTRY_CONFIG'), SCRIPT.index('https://get.k3s.io'))
        with tempfile.TemporaryDirectory() as directory:
            local_block = block.replace('/etc/rancher/k3s', directory)
            # Ownership is applied in production; a local test cannot chown root.
            for token in ('ghp_first_fake_token', 'github_pat_second_fake_token'):
                with patch.dict(os.environ, {'GHCR_PAT': token}):
                    rendered = CloudProvider.substitute_startup_script_vars(local_block)
                result = subprocess.run(['bash', '-c', 'set -ex; chown() { :; };\n' + rendered],
                                        capture_output=True, text=True, check=True)
                self.assertNotIn(token, result.stdout + result.stderr)
                target = Path(directory) / 'registries.yaml'
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                config = yaml.safe_load(target.read_text())
                self.assertEqual(config['configs']['ghcr.io']['auth'],
                                 {'username': 'agentsanchez', 'password': token})
                self.assertEqual(config['mirrors']['ghcr.io']['endpoint'], ['https://ghcr.io'])
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_gpu_has_same_registry_setup_before_k3s(self):
        gpu = (ROOT / 'scripts/gpu-startup.sh').read_text()
        block = SCRIPT[SCRIPT.index('# Install registry credentials'):SCRIPT.index('export INSTALL_K3S_VERSION')]
        self.assertIn(block, gpu)
        self.assertLess(gpu.index('GHCR_REGISTRY_CONFIG'), gpu.index('https://get.k3s.io'))
        self.assertNotIn('set -eux', gpu)

    def test_missing_empty_and_malformed_pat_fail_before_launch(self):
        for environment in ({}, {'GHCR_PAT': ''}, {'GHCR_PAT': 'bad"\nvalue'}):
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    CloudProvider.substitute_startup_script_vars('__GHCR_PAT__')

    def test_env_references_secret_and_external_secret_uses_existing_store(self):
        patch_data = json.loads((ROOT / 'tests/fixtures/ghcr-env-patch.json').read_text())
        env = patch_data['spec']['template']['spec']['containers'][0]['env'][0]
        self.assertEqual(env['name'], 'GHCR_PAT')
        self.assertNotIn('value', env)
        external = yaml.safe_load((ROOT / 'tests/fixtures/ghcr-external-secret.yaml').read_text())
        self.assertEqual(env['valueFrom']['secretKeyRef']['name'], external['spec']['target']['name'])
        self.assertEqual(external['spec']['data'][0]['remoteRef']['property'], 'ghcr-pat')
