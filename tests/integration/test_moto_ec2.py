import base64
import logging
import unittest

import boto3
from botocore.config import Config
from moto.server import ThreadedMotoServer

from ec2 import EC2Provider
from support import completion


class MotoFleetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        cls.server = ThreadedMotoServer(ip_address='127.0.0.1', port=0, verbose=False)
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        host, port = cls.server.get_host_and_port()
        cls.client = boto3.client('ec2', endpoint_url=f'http://{host}:{port}', region_name='us-west-2',
            aws_access_key_id='test', aws_secret_access_key='test', aws_session_token='test',
            config=Config(retries={'max_attempts': 0}, connect_timeout=2, read_timeout=3, proxies={}))
        vpc = cls.client.create_vpc(CidrBlock='10.20.0.0/16')['Vpc']['VpcId']
        cls.subnets = [cls.client.create_subnet(VpcId=vpc, CidrBlock=f'10.20.{i}.0/24',
                      AvailabilityZone=zone)['Subnet']['SubnetId']
                      for i, zone in enumerate(['us-west-2a', 'us-west-2b'])]
        cls.template = cls.client.create_launch_template(LaunchTemplateName='local-gpu',
            LaunchTemplateData={'ImageId': 'ami-12345678', 'InstanceType': 'p5.4xlarge'})['LaunchTemplate']['LaunchTemplateId']

    def test_provider_spot_and_on_demand_lifecycle(self):
        for market in ('spot', 'on-demand'):
            with self.subTest(market=market):
                provider = EC2Provider(client=self.client, region='us-west-2', template=self.template,
                    subnet_ids=self.subnets, capacity_type=market, name_prefix='moto-gpu',
                    startup_script='#!/bin/sh\necho {{INSTANCE_NAME}}')
                name, result = completion(self, provider.create_instance)
                self.assertTrue(result['success'], str(result.get('error')))
                self.assertIn(name, provider.list_instances())
                instances = list(provider._instances(name))
                self.assertEqual(len(instances), 1)
                identifier = instances[0]['InstanceId']
                self.assertEqual(instances[0].get('InstanceLifecycle', 'on-demand'), market)
                _, deleted = completion(self, lambda **kw: provider.delete_instance(name, **kw))
                self.assertTrue(deleted['success'])
                self.assertNotIn(name, provider.list_instances())
                self.assertEqual(self.client.describe_instances(InstanceIds=[identifier])['Reservations'][0]
                                 ['Instances'][0]['State']['Name'], 'terminated')

    def test_inventory_cannot_delete_another_pool(self):
        provider = EC2Provider(client=self.client, region='us-west-2', template=self.template,
            subnet_ids=self.subnets, name_prefix='moto-isolated')
        with self.assertRaises(ValueError):
            provider.delete_instance('another-pool-instance')

    @unittest.expectedFailure
    def test_moto_does_not_yet_apply_fleet_per_launch_user_data(self):
        """Known Moto 5.1.17 gap; unexpected success signals this marker can be removed.

        Our exact AWS request still has independent botocore Stubber coverage.
        Never change the production request merely to satisfy emulator behavior.
        """
        script = '#!/bin/sh\necho local-integration-test'
        provider = EC2Provider(client=self.client, region='us-west-2', template=self.template,
            subnet_ids=self.subnets, capacity_type='spot', name_prefix='moto-userdata', startup_script=script)
        name, result = completion(self, provider.create_instance)
        self.assertTrue(result['success'])
        try:
            identifier = next(provider._instances(name))['InstanceId']
            actual = self.client.describe_instance_attribute(InstanceId=identifier, Attribute='userData')
            self.assertEqual(actual.get('UserData', {}).get('Value'), base64.b64encode(script.encode()).decode())
        finally:
            _, result = completion(self, lambda **kw: provider.delete_instance(name, **kw))
            self.assertTrue(result['success'])
