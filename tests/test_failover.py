import unittest
from unittest.mock import Mock

from failover import OrderedProvider


def provider(name):
    p = Mock(PROVIDER_NAME=name, NAME_PREFIX='emergency-' + name)
    p.list_instances.return_value = []
    return p


class FailoverTests(unittest.TestCase):
    def test_failed_launch_moves_to_next_provider(self):
        ec2, gce = provider('ec2'), provider('gce')
        ordered = OrderedProvider([ec2, gce], 'emergency')
        ordered.create_instance()
        callback = ec2.create_instance.call_args.args[0]
        callback(False, 'emergency-ec2-1', RuntimeError('capacity'))
        ordered.create_instance()
        gce.create_instance.assert_called_once()

    def test_inventory_failure_blocks_launch(self):
        ec2, gce = provider('ec2'), provider('gce')
        ec2.list_instances.side_effect = RuntimeError('inventory unavailable')
        ordered = OrderedProvider([ec2, gce], 'emergency')
        with self.assertRaises(RuntimeError):
            ordered.list_instances()
        gce.create_instance.assert_not_called()

    def test_delete_routes_to_instance_provider_after_restart(self):
        ec2, gce = provider('ec2'), provider('gce')
        OrderedProvider([ec2, gce], 'emergency').delete_instance('emergency-gce-abc')
        gce.delete_instance.assert_called_once_with('emergency-gce-abc', None)
        ec2.delete_instance.assert_not_called()
