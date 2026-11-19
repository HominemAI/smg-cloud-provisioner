from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from kubernetes import client
import k8s


def pod(name, owner=None, terminating=False):
    return NS(metadata=NS(name=name, namespace='apps', uid=name,
                          owner_references=[NS(kind=owner)] if owner else [],
                          annotations={}, deletion_timestamp='now' if terminating else None),
              spec=NS(volumes=[]), status=NS(phase='Running'))


class DrainTests(unittest.TestCase):
    def test_cordon_evict_and_wait_respecting_pdb(self):
        api = Mock()
        api.list_pod_for_all_namespaces.return_value.items = [pod('app'), pod('daemon', 'DaemonSet')]
        api.create_namespaced_pod_eviction.side_effect = client.exceptions.ApiException(status=429)
        with patch.object(k8s, 'v1', api):
            self.assertFalse(k8s.drain_node(NS(metadata=NS(name='emergency-1'))))
            api.patch_node.assert_called_once_with('emergency-1', {'spec': {'unschedulable': True}})
            self.assertEqual(api.create_namespaced_pod_eviction.call_count, 1)
            api.list_pod_for_all_namespaces.return_value.items = [pod('daemon', 'DaemonSet')]
            self.assertTrue(k8s.drain_node(NS(metadata=NS(name='emergency-1'))))

    def test_terminating_pod_still_blocks_termination(self):
        api = Mock()
        api.list_pod_for_all_namespaces.return_value.items = [pod('app', terminating=True)]
        with patch.object(k8s, 'v1', api):
            self.assertFalse(k8s.drain_node(NS(metadata=NS(name='emergency-1'))))
        api.create_namespaced_pod_eviction.assert_not_called()
