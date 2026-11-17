import os
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from async_tools import AsyncSet
from cloud_provider import CloudProvider
import k8s
from provisioner import build_pools, Pool

SCRIPT = Path('scripts/gpu-ec2-startup.sh').read_text()


class EC2GpuTests(unittest.TestCase):
    def test_gpu_demand_is_routed_once(self):
        pods = AsyncSet()
        for uid, selector, node in [('default', None, None), ('aws', {'smgp-role':'gpu-aws'}, None),
                                    ('gce', {'smgp-role':'gpu'}, None), ('other', {'smgp-role':'other'}, None),
                                    ('running', None, 'aws-node')]:
            pods.add(NS(metadata=NS(uid=uid),spec=NS(node_selector=selector,node_name=node)))
        nodes = [NS(metadata=NS(name='aws-node',labels={'smgp-role':'gpu-aws'}))]
        with patch.object(k8s,'GPU_PODS',pods), patch.object(k8s,'ALL_NODES',nodes):
            self.assertEqual(k8s.get_current_gpu_pod_count('gpu'),2)
            self.assertEqual(k8s.get_current_gpu_pod_count('gpu-aws'),2)
            self.assertEqual(k8s.get_current_gpu_pod_count('other'),1)

    def test_additional_pool_configuration(self):
        env={'GPU_ENABLED':'false','EMERGENCY_ENABLED':'false','GPU_EC2_ENABLED':'true',
             'GPU_EC2_LAUNCH_TEMPLATE':'lt-gpu','AWS_REGION':'us-west-2','AWS_SUBNET_IDS':'subnet-a',
             'DRY_RUN':'true'}
        with patch.dict(os.environ,env,clear=True), patch('ec2.boto3.client'):
            pool, = build_pools()
        self.assertEqual(pool.role,'gpu-aws')
        self.assertEqual(pool.provider.capacity_type,'spot')
        self.assertEqual(pool.max_nodes,1)
        self.assertEqual(pool.provisioning_timeout,1200)
        self.assertIn('--node-taint nvidia.com/gpu=gpu-aws:NoSchedule',pool.provider.startup_script)
        self.assertNotIn('__GPU_EC2_ROLE__',pool.provider.startup_script)
        self.assertTrue(pool.dry_run)

    def test_rendered_bootstrap_fits_userdata_and_bash(self):
        required,_=CloudProvider.parse_startup_script(SCRIPT)
        env={key:'placeholder' for key in required}
        env['GHCR_PAT']='ghp_fake_test'
        with patch.dict(os.environ,env):
            rendered=CloudProvider.substitute_startup_script_vars(SCRIPT,{'INSTANCE_NAME':'smg-gpu-aws-test','INSTANCE_PROVIDER':'ec2'})
        self.assertLess(len(rendered.encode()),16384)
        subprocess.run(['bash','-n'],input=rendered,text=True,check=True)
        self.assertLess(rendered.index('GHCR_REGISTRY_CONFIG'), rendered.index('https://get.k3s.io'))

    def test_storage_refuses_ebs_only_and_partitioned_instance_disk(self):
        block=SCRIPT[SCRIPT.index('LOCAL_DISK=""'):SCRIPT.index('mountpoint -q "$LOCAL_DISK"')]
        for disk_listing in ('/dev/nvme0n1 Amazon Elastic Block Store',
                             '/dev/nvme1n1 Amazon EC2 NVMe Instance Storage'):
            # EBS never becomes a candidate; partitioned instance storage fails
            # the child-device check before blkid/mkfs/mount can be called.
            mocks='''mountpoint() { return 1; }
lsblk() {
  if [ "$1" = '-dn' ]; then printf '%s\\n' 'DISK_LISTING';
  else printf 'nvme1n1\\nnvme1n1p1\\n'; fi
}
mkfs.ext4() { echo UNSAFE_FORMAT; }
'''.replace('DISK_LISTING',disk_listing)
            result=subprocess.run(['bash','-euc',mocks+block],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('UNSAFE_FORMAT',result.stdout)

    def test_gpu_instance_that_never_joins_is_removed(self):
        provider=Mock(NAME_PREFIX='smg-gpu-aws',PROVIDER_NAME='ec2')
        provider.list_instances.return_value=['smg-gpu-aws-lost']
        pool=Pool('gpu-ec2',provider,'gpu-aws',1,provisioning_timeout=10,clock=lambda:0)
        with patch('k8s.pool_nodes',return_value=[]),patch('k8s.get_current_gpu_pod_count',return_value=1):
            pool.reconcile()
            pool.clock=lambda:11
            pool.reconcile()
        provider.delete_instance.assert_called_once()
        provider.create_instance.assert_not_called()
