#!/usr/bin/env python3
"""Run local integration tests with outbound cloud connections forbidden."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests' / 'integration'))

if __name__ == '__main__':
    os.chdir(ROOT)
    # Explicit fake client credentials are also supplied in each test. Disable
    # metadata/proxy discovery so local tests never inherit an ambient identity.
    os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
    os.environ['NO_PROXY'] = '127.0.0.1,localhost,::1'
    from support import local_network_only
    with local_network_only():
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests' / 'integration'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
