"""Single-region EC2 instant Fleets, isolated by a pool-specific identifier tag."""
import base64
import logging
import os
import threading
import uuid

import boto3
from botocore.exceptions import ClientError

from cloud_provider import CloudProvider
from instance_audit import record

logger = logging.getLogger(__name__)
IDENTIFIER_TAG = 'smgp-instance-identifier'


class FleetLaunchError(RuntimeError):
    """A Fleet request completed without fulfilling exactly one instance."""


class EC2Provider(CloudProvider):
    PROVIDER_NAME = 'ec2'

    def __init__(self, region=None, template=None, version=None, client=None,
                 subnet_ids=None, capacity_type=None, **kwargs):
        super().__init__(**kwargs)
        region = region or os.getenv('AWS_REGION')
        template = template or os.getenv('EC2_LAUNCH_TEMPLATE')
        if not region or not template:
            raise ValueError('AWS_REGION and EC2_LAUNCH_TEMPLATE are required')
        raw_subnets = subnet_ids if subnet_ids is not None else os.getenv('AWS_SUBNET_IDS', '')
        if isinstance(raw_subnets, str):
            raw_subnets = raw_subnets.split(',')
        self.subnet_ids = list(dict.fromkeys(s.strip() for s in raw_subnets if s.strip()))
        if not self.subnet_ids:
            raise ValueError('AWS_SUBNET_IDS must list at least one subnet in AWS_REGION')
        self.explicit_capacity_type = capacity_type or os.getenv('EC2_CAPACITY_TYPE')
        self.capacity_type = self.explicit_capacity_type or 'on-demand'
        self.template_market = None
        if self.capacity_type not in ('spot', 'on-demand'):
            raise ValueError('EC2_CAPACITY_TYPE must be spot or on-demand')
        self.region = region
        self.template = {
            'LaunchTemplateId' if template.startswith('lt-') else 'LaunchTemplateName': template,
            'Version': version or os.getenv('EC2_LAUNCH_TEMPLATE_VERSION', '$Default'),
        }
        self.client = client or boto3.client('ec2', region_name=region)

    @staticmethod
    def _submit(operation, name, callback):
        try:
            operation()
        except Exception as error:
            if isinstance(error, ClientError):
                code = error.response['Error']['Code']
                if code in ('InsufficientInstanceCapacity', 'InsufficientHostCapacity', 'InstanceLimitExceeded', 'VcpuLimitExceeded', 'UnauthorizedOperation', 'AuthFailure', 'AccessDenied'):
                    error.launch_rejected = True
                category = ('capacity' if code in ('InsufficientInstanceCapacity', 'InsufficientHostCapacity')
                            else 'quota' if code in ('InstanceLimitExceeded', 'VcpuLimitExceeded')
                            else 'authorization' if code in ('UnauthorizedOperation', 'AuthFailure', 'AccessDenied')
                            else 'provider')
                logger.error('EC2 %s error for %s: %s', category, name, code)
            elif isinstance(error, (FleetLaunchError, ValueError)):
                logger.error('EC2 launch failed for %s: %s', name, error)
            else:
                logger.error('EC2 operation failed for %s (%s)', name, type(error).__name__)
            if callback:
                callback(success=False, instance_name=name, error=error)
        else:
            if callback:
                callback(success=True, instance_name=name, error=None)

    def _launch_configuration(self):
        """Resolve one template version and filter configured subnets by offering."""
        lookup = {k: v for k, v in self.template.items() if k != 'Version'}
        response = self.client.describe_launch_template_versions(
            **lookup, Versions=[self.template['Version']])
        version = response['LaunchTemplateVersions'][0]
        data = version['LaunchTemplateData']
        instance_type = data.get('InstanceType')
        if not instance_type:
            raise ValueError('The EC2 launch template must specify InstanceType')
        placement = data.get('Placement', {})
        if any(placement.get(k) for k in ('AvailabilityZone', 'AvailabilityZoneId', 'GroupName', 'GroupId', 'HostId', 'HostResourceGroupArn')):
            raise ValueError('Remove fixed AZ, placement group or host from the Fleet launch template')
        if data.get('NetworkInterfaces'):
            raise ValueError('Use template SecurityGroupIds and subnet public-IP settings; remove NetworkInterfaces for Fleet')
        self.capacity_type = self.explicit_capacity_type or 'on-demand'
        self.template_market = data.get('InstanceMarketOptions')
        if self.template_market:
            mode = self.template_market.get('MarketType')
            if mode not in ('spot', 'on-demand') or (self.explicit_capacity_type and self.explicit_capacity_type != mode):
                raise ValueError('Explicit capacity mode conflicts with template market settings')
            spot = self.template_market.get('SpotOptions', {})
            if spot.get('SpotInstanceType', 'one-time') != 'one-time' or spot.get('InstanceInterruptionBehavior', 'terminate') != 'terminate':
                raise ValueError('Persistent or stop/hibernate Spot requests require a separate request lifecycle')
            self.capacity_type = mode
        if data.get('CapacityReservationSpecification', {}).get('CapacityReservationTarget'):
            raise ValueError('Remove the fixed capacity reservation from the Fleet launch template')

        subnets = []
        for page in self.client.get_paginator('describe_subnets').paginate(SubnetIds=self.subnet_ids):
            subnets.extend(page['Subnets'])
        if {s['SubnetId'] for s in subnets} != set(self.subnet_ids):
            raise ValueError('Some AWS_SUBNET_IDS were not found in AWS_REGION')
        if len({s['VpcId'] for s in subnets}) != 1:
            raise ValueError('AWS_SUBNET_IDS must belong to one VPC')
        zones = sorted({s['AvailabilityZoneId'] for s in subnets if s['State'] == 'available'})
        if not zones:
            raise ValueError('No configured subnet is available')
        offered = set()
        for page in self.client.get_paginator('describe_instance_type_offerings').paginate(
            LocationType='availability-zone-id', Filters=[
                {'Name': 'instance-type', 'Values': [instance_type]},
                {'Name': 'location', 'Values': zones},
            ],
        ):
            offered.update(o['Location'] for o in page['InstanceTypeOfferings'])
        eligible = [s['SubnetId'] for s in subnets
                    if s['State'] == 'available' and s['AvailabilityZoneId'] in offered]
        if not eligible:
            raise ValueError(f'{instance_type} is not offered in any configured subnet AZ')
        # Pin $Default/$Latest for this attempt, including any SDK retries.
        specification = dict(LaunchTemplateId=version['LaunchTemplateId'], Version=str(version['VersionNumber']))
        return specification, [{'SubnetId': subnet, 'InstanceType': instance_type,
                                'WeightedCapacity': 1.0} for subnet in eligible]

    def create_instance(self, callback=None):
        name = f'{self.NAME_PREFIX}-{uuid.uuid4().hex[:8]}'
        script = self.substitute_startup_script_vars(self.startup_script or '', {
            'INSTANCE_NAME': name, 'INSTANCE_PROVIDER': self.PROVIDER_NAME,
        }).encode('utf-8')
        if len(script) > 16384:
            raise ValueError('EC2 startup script exceeds the 16 KiB user-data limit')

        def launch():
            specification, overrides = self._launch_configuration()
            tags = [{'Key': 'Name', 'Value': name}, {'Key': IDENTIFIER_TAG, 'Value': name}]
            if self.template_market:
                # Fleet does not accept template InstanceMarketOptions. Native
                # RunInstances honors the entire template purchasing specification.
                # One explicit subnet per attempt; ordered candidates provide fallback.
                subnet = next(x['SubnetId'] for identifier in self.subnet_ids
                              for x in overrides if x['SubnetId'] == identifier)
                record('create_requested', self.PROVIDER_NAME, name, region=self.region,
                       client_token=name, subnet_ids=[subnet], capacity_type='template')
                result = self.client.run_instances(LaunchTemplate=specification,
                    MinCount=1, MaxCount=1, ClientToken=name, SubnetId=subnet,
                    UserData=script.decode('utf-8'),
                    TagSpecifications=[{'ResourceType': 'instance', 'Tags': tags}])
                ids = [item['InstanceId'] for item in result.get('Instances', [])]
                record('created', self.PROVIDER_NAME, name, region=self.region, instance_ids=ids)
                if len(ids) != 1:
                    raise FleetLaunchError('Template launch did not return exactly one instance')
                return
            specification['LaunchTemplateSpecificationUserData'] = base64.b64encode(script).decode('ascii')
            request = dict(
                Type='instant', ClientToken=name,
                LaunchTemplateConfigs=[{'LaunchTemplateSpecification': specification, 'Overrides': overrides}],
                TargetCapacitySpecification={
                    'TotalTargetCapacity': 1, 'TargetCapacityUnitType': 'units',
                    'DefaultTargetCapacityType': self.capacity_type,
                    'OnDemandTargetCapacity': int(self.capacity_type == 'on-demand'),
                    'SpotTargetCapacity': int(self.capacity_type == 'spot'),
                },
                TagSpecifications=[{'ResourceType': resource, 'Tags': tags}
                                   for resource in ('fleet', 'instance')],
            )
            if self.capacity_type == 'spot':
                request['SpotOptions'] = {
                    'AllocationStrategy': 'price-capacity-optimized',
                    'InstanceInterruptionBehavior': 'terminate',
                    'MinTargetCapacity': 1,
                    'SingleInstanceType': True,
                }
            else:
                request['OnDemandOptions'] = {'AllocationStrategy': 'lowest-price', 'MinTargetCapacity': 1, 'SingleInstanceType': True}
            record('create_requested', self.PROVIDER_NAME, name, region=self.region,
                   client_token=name, capacity_type=self.capacity_type,
                   subnet_ids=[o['SubnetId'] for o in overrides])
            result = self.client.create_fleet(**request)
            ids = list(dict.fromkeys(instance_id for group in result.get('Instances', [])
                                     for instance_id in group.get('InstanceIds', [])))
            errors = [e.get('ErrorCode', 'Unknown') for e in result.get('Errors', [])]
            record('fleet_result', self.PROVIDER_NAME, name, region=self.region,
                   fleet_id=result.get('FleetId'), instance_ids=ids, error_codes=errors)
            if len(ids) != 1:
                if ids:
                    # Defensive: never leave unexpected extra capacity running.
                    record('delete_requested', self.PROVIDER_NAME, name, region=self.region, instance_ids=ids)
                    self.client.terminate_instances(InstanceIds=ids)
                    record('termination_submitted', self.PROVIDER_NAME, name, region=self.region, instance_ids=ids)
                error = FleetLaunchError('Expected one Fleet instance; returned %s; errors=%s' % (len(ids), errors))
                error.launch_rejected = not ids and bool(errors)
                raise error
            # Errors in other AZs do not invalidate a successful one-instance launch.
            record('created', self.PROVIDER_NAME, name, region=self.region,
                   fleet_id=result.get('FleetId'), instance_ids=ids)

        threading.Thread(target=self._submit, args=(launch, name, callback)).start()
        return name

    def _instances(self, identifier=None):
        filters = [
            {'Name': f'tag:{IDENTIFIER_TAG}', 'Values': [identifier or f'{self.NAME_PREFIX}-*']},
            {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped', 'shutting-down']},
        ]
        for page in self.client.get_paginator('describe_instances').paginate(Filters=filters):
            for reservation in page['Reservations']:
                yield from reservation['Instances']

    def list_instances(self):
        return [tag['Value'] for instance in self._instances()
                for tag in instance.get('Tags', []) if tag['Key'] == IDENTIFIER_TAG]

    def get_instance(self, instance_identifier):
        if not instance_identifier.startswith(self.NAME_PREFIX + '-'):
            return None
        return instance_identifier if next(self._instances(instance_identifier), None) else None

    def delete_instance(self, instance_identifier, callback=None):
        if not instance_identifier.startswith(self.NAME_PREFIX + '-'):
            raise ValueError('Instance is outside this pool')

        def terminate():
            ids = [instance['InstanceId'] for instance in self._instances(instance_identifier)]
            record('delete_requested', self.PROVIDER_NAME, instance_identifier, region=self.region, instance_ids=ids)
            if ids:
                self.client.terminate_instances(InstanceIds=ids)
            record('termination_submitted', self.PROVIDER_NAME, instance_identifier, region=self.region, instance_ids=ids)

        threading.Thread(target=self._submit, args=(terminate, instance_identifier, callback)).start()
        return True
