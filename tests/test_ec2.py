import base64
import threading
import unittest
from unittest.mock import patch

import boto3
from botocore.stub import Stubber, ANY
from ec2 import EC2Provider


class EC2Tests(unittest.TestCase):
    def setUp(self):
        client = boto3.client('ec2', region_name='us-west-2', aws_access_key_id='test', aws_secret_access_key='test')
        self.stub = Stubber(client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.provider = EC2Provider(client=client, region='us-west-2', template='lt-test',
                                    subnet_ids=['subnet-a', 'subnet-b'],
                                    name_prefix='emergency', startup_script='#!/bin/sh\necho {{INSTANCE_NAME}}')
        self.result = None
        self.done = threading.Event()

    def callback(self, **result):
        self.result = result
        self.done.set()

    def wait(self):
        self.assertTrue(self.done.wait(3), 'callback missing')
        self.stub.assert_no_pending_responses()

    def filters(self, name):
        return {'Filters': [{'Name': 'tag:smgp-instance-identifier', 'Values': [name]},
                            {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped', 'shutting-down']}]}

    def preflight(self, offered=('usw2-az1', 'usw2-az2'), template_data=None, subnets=None):
        data = template_data if template_data is not None else {'InstanceType': 'p5.4xlarge'}
        self.stub.add_response('describe_launch_template_versions', {
            'LaunchTemplateVersions': [{'LaunchTemplateId': 'lt-test', 'VersionNumber': 3, 'LaunchTemplateData': data}],
        }, {'LaunchTemplateId': 'lt-test', 'Versions': ['$Default']})
        if any(data.get(k) for k in ('Placement', 'NetworkInterfaces', 'CapacityReservationSpecification')) or not data.get('InstanceType'):
            return
        subnet_data = subnets if subnets is not None else [
            {'SubnetId': 'subnet-a', 'VpcId': 'vpc-test', 'AvailabilityZoneId': 'usw2-az1', 'State': 'available'},
            {'SubnetId': 'subnet-b', 'VpcId': 'vpc-test', 'AvailabilityZoneId': 'usw2-az2', 'State': 'available'},
        ]
        self.stub.add_response('describe_subnets', {'Subnets': subnet_data}, {'SubnetIds': ['subnet-a', 'subnet-b']})
        if len({s['VpcId'] for s in subnet_data}) != 1:
            return
        self.stub.add_response('describe_instance_type_offerings', {'InstanceTypeOfferings': [
            {'InstanceType': 'p5.4xlarge', 'LocationType': 'availability-zone-id', 'Location': zone} for zone in offered
        ]}, {'LocationType': 'availability-zone-id', 'Filters': [
            {'Name': 'instance-type', 'Values': ['p5.4xlarge']},
            {'Name': 'location', 'Values': ['usw2-az1', 'usw2-az2']},
        ]})

    def launch(self):
        with patch('ec2.uuid.uuid4') as uuid:
            uuid.return_value.hex = '12345678'
            name = self.provider.create_instance(self.callback)
        self.wait()
        return name

    def expected_request(self, capacity_type='on-demand', subnets=('subnet-a', 'subnet-b')):
        name = 'emergency-12345678'
        tags = [{'Key': 'Name', 'Value': name}, {'Key': 'smgp-instance-identifier', 'Value': name}]
        request = {
            'Type': 'instant', 'ClientToken': name,
            'LaunchTemplateConfigs': [{
                'LaunchTemplateSpecification': {'LaunchTemplateId': 'lt-test', 'Version': '3',
                    'LaunchTemplateSpecificationUserData': base64.b64encode(('#!/bin/sh\necho ' + name).encode()).decode()},
                'Overrides': [{'SubnetId': subnet, 'InstanceType': 'p5.4xlarge', 'WeightedCapacity': 1.0} for subnet in subnets],
            }],
            'TargetCapacitySpecification': {'TotalTargetCapacity': 1, 'TargetCapacityUnitType': 'units',
                'DefaultTargetCapacityType': capacity_type, 'OnDemandTargetCapacity': int(capacity_type == 'on-demand'),
                'SpotTargetCapacity': int(capacity_type == 'spot')},
            'TagSpecifications': [{'ResourceType': r, 'Tags': tags} for r in ('fleet', 'instance')],
        }
        if capacity_type == 'spot':
            request['SpotOptions'] = {'AllocationStrategy': 'price-capacity-optimized',
                                     'InstanceInterruptionBehavior': 'terminate', 'MinTargetCapacity': 1, 'SingleInstanceType': True}
        else:
            request['OnDemandOptions'] = {'AllocationStrategy': 'lowest-price', 'MinTargetCapacity': 1, 'SingleInstanceType': True}
        return request

    def test_create_on_demand_across_subnets(self):
        self.preflight()
        self.stub.add_response('create_fleet', {'FleetId': 'fleet-1', 'Instances': [{'InstanceIds': ['i-123']}]},
                               self.expected_request())
        self.assertEqual(self.launch(), 'emergency-12345678')
        self.assertTrue(self.result['success'])

    def test_spot_filters_unsupported_az_and_accepts_success_with_other_errors(self):
        self.provider.explicit_capacity_type = 'spot'
        self.preflight(offered=('usw2-az2',))
        self.stub.add_response('create_fleet', {'FleetId': 'fleet-1', 'Instances': [{'InstanceIds': ['i-123']}],
            'Errors': [{'ErrorCode': 'InsufficientInstanceCapacity'}]}, self.expected_request('spot', ('subnet-b',)))
        self.launch()
        self.assertTrue(self.result['success'])

    def test_capacity_error_in_successful_http_response(self):
        self.preflight()
        self.stub.add_response('create_fleet', {'FleetId': 'fleet-1', 'Instances': [],
            'Errors': [{'ErrorCode': 'InsufficientInstanceCapacity'}]}, self.expected_request())
        self.launch()
        self.assertFalse(self.result['success'])

    def test_capacity_api_error(self):
        self.preflight()
        self.stub.add_client_error('create_fleet', service_error_code='InsufficientInstanceCapacity', expected_params=self.expected_request())
        self.launch()
        self.assertFalse(self.result['success'])

    def test_authorization_error(self):
        self.preflight()
        self.stub.add_client_error('create_fleet', service_error_code='UnauthorizedOperation', expected_params=self.expected_request())
        self.launch()
        self.assertFalse(self.result['success'])

    def test_no_offering_does_not_launch(self):
        self.preflight(offered=())
        self.launch()
        self.assertFalse(self.result['success'])

    def test_template_with_fixed_az_rejected(self):
        self.preflight(template_data={'InstanceType': 'p5.4xlarge', 'Placement': {'AvailabilityZone': 'us-west-2a'}})
        self.launch()
        self.assertFalse(self.result['success'])

    def test_template_with_network_interface_rejected(self):
        self.preflight(template_data={'InstanceType': 'p5.4xlarge', 'NetworkInterfaces': [{'DeviceIndex': 0, 'SubnetId': 'subnet-a'}]})
        self.launch()
        self.assertFalse(self.result['success'])

    def test_template_market_options_honored(self):
        self.preflight(template_data={'InstanceType': 'p5.4xlarge', 'InstanceMarketOptions': {'MarketType': 'spot'}})
        self.stub.add_response('run_instances', {'Instances': [{'InstanceId': 'i-template'}]}, {
            'LaunchTemplate': {'LaunchTemplateId': 'lt-test', 'Version': '3'},
            'MinCount': 1, 'MaxCount': 1, 'ClientToken': ANY, 'SubnetId': 'subnet-a',
            'UserData': ANY, 'TagSpecifications': ANY})
        self.launch()
        self.assertTrue(self.result['success'])

    def test_subnets_must_share_vpc(self):
        self.preflight(subnets=[
            {'SubnetId': 'subnet-a', 'VpcId': 'vpc-a', 'AvailabilityZoneId': 'usw2-az1', 'State': 'available'},
            {'SubnetId': 'subnet-b', 'VpcId': 'vpc-b', 'AvailabilityZoneId': 'usw2-az2', 'State': 'available'},
        ])
        self.launch()
        self.assertFalse(self.result['success'])

    def test_unexpected_multiple_instances_are_all_terminated(self):
        self.preflight()
        self.stub.add_response('create_fleet', {'FleetId': 'fleet-1', 'Instances': [{'InstanceIds': ['i-1', 'i-2']}]}, self.expected_request())
        self.stub.add_response('terminate_instances', {'TerminatingInstances': []}, {'InstanceIds': ['i-1', 'i-2']})
        self.launch()
        self.assertFalse(self.result['success'])

    def test_oversized_user_data_rejected_before_any_api_request(self):
        self.provider.startup_script = 'x' * 16385
        with self.assertRaises(ValueError):
            self.provider.create_instance()

    def test_missing_subnets_and_invalid_capacity_type(self):
        for kwargs in ({'subnet_ids': []}, {'subnet_ids': ['subnet-a'], 'capacity_type': 'invalid'}):
            with self.assertRaises(ValueError):
                EC2Provider(region='us-west-2', template='lt-test', client=self.provider.client, **kwargs)

    def test_delete(self):
        self.stub.add_response('describe_instances', {'Reservations': [{'Instances': [{'InstanceId': 'i-123'}]}]}, self.filters('emergency-1'))
        self.stub.add_response('terminate_instances', {'TerminatingInstances': []}, {'InstanceIds': ['i-123']})
        self.provider.delete_instance('emergency-1', self.callback)
        self.wait()
        self.assertTrue(self.result['success'])

    def test_delete_missing(self):
        self.stub.add_response('describe_instances', {'Reservations': []}, self.filters('emergency-1'))
        self.provider.delete_instance('emergency-1', self.callback)
        self.wait()
        self.assertTrue(self.result['success'])

    def test_list_paginates(self):
        self.stub.add_response('describe_instances', {'Reservations': [], 'NextToken': 'next'}, self.filters('emergency-*'))
        self.stub.add_response('describe_instances', {'Reservations': [{'Instances': [{'Tags': [
            {'Key': 'smgp-instance-identifier', 'Value': 'emergency-1'}]}]}]},
            dict(self.filters('emergency-*'), NextToken='next'))
        self.assertEqual(self.provider.list_instances(), ['emergency-1'])
        self.stub.assert_no_pending_responses()

    def test_delete_other_pool_rejected(self):
        with self.assertRaises(ValueError):
            self.provider.delete_instance('gpu-1')
