import threading
import unittest
from unittest.mock import Mock, patch
from gce import GCEProvider


class GCETests(unittest.TestCase):
    def test_pool_settings_template_region_and_script(self):
        client = Mock()
        done = threading.Event()
        provider = GCEProvider(project='project', zone='europe-west4-a', template='cpu',
                               name_prefix='emergency', startup_script='echo {{INSTANCE_NAME}}')
        with patch('gce.compute_v1.InstancesClient', return_value=client):
            name = provider.create_instance(callback=lambda **_: done.set())
            self.assertTrue(done.wait(2))
        request = client.insert.call_args.kwargs['request']
        self.assertEqual(request.source_instance_template, 'projects/project/regions/europe-west4/instanceTemplates/cpu')
        self.assertEqual(request.instance_resource.metadata.items[0].value, 'echo ' + name)

    def test_inventory_isolation(self):
        client = Mock()
        client.list.return_value = [Mock(name='gpu-1'), Mock(name='emergency-1')]
        # Mock.name is itself a Mock, so use simple objects for SDK names.
        from types import SimpleNamespace
        client.list.return_value = [SimpleNamespace(name='gpu-1'), SimpleNamespace(name='emergency-1')]
        provider = GCEProvider(project='project', zone='us-west1-a', template='cpu', name_prefix='emergency')
        with patch('gce.compute_v1.InstancesClient', return_value=client):
            self.assertEqual(provider.list_instances(), ['emergency-1'])
        with self.assertRaises(ValueError):
            provider.delete_instance('gpu-1')
