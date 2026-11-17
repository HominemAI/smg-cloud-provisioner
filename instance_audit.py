"""Append-only instance lifecycle ledger; never records credentials or user data."""
import json
import logging
import os
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
logger = logging.getLogger(__name__)


def record(event, provider, name, **details):
    entry = dict(time=datetime.now(timezone.utc).isoformat(), event=event,
                 provider=provider, name=name, **details)
    line = json.dumps(entry, sort_keys=True)
    path = os.getenv('INSTANCE_AUDIT_LOG')
    if path:
        with _lock, open(path, 'a', encoding='utf-8') as stream:
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    logger.info('INSTANCE_AUDIT %s', line)
