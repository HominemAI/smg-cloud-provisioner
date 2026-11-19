import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from instance_audit import record


class AuditTests(unittest.TestCase):
    def test_append_preserves_previous_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instances.jsonl'
            with patch.dict(os.environ, {'INSTANCE_AUDIT_LOG': str(path)}):
                record('create_requested', 'gce', 'emergency-gce-one', project='test', zone='us-west1-a')
                record('created', 'gce', 'emergency-gce-one', project='test', zone='us-west1-a')
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e['event'] for e in entries], ['create_requested', 'created'])
            self.assertTrue(all(e['name'] == 'emergency-gce-one' for e in entries))

    def test_missing_ledger_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'INSTANCE_AUDIT_LOG': str(Path(directory) / 'missing' / 'instances.jsonl')}):
                with self.assertRaises(FileNotFoundError):
                    record('create_requested', 'gce', 'emergency-gce-one')
