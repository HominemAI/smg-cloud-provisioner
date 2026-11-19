"""Small lifecycle helpers shared by resource-aware cloud adapters."""
import re
import time

from lifecycle import UnresolvedLaunch


def validate_prefix(prefix):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}[a-z0-9]', prefix):
        raise ValueError('Pool prefix must be 2–32 lowercase letters/digits/hyphens')


def check_record(prefix, name, resources, planned):
    if not re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{16}', name):
        raise ValueError('Instance is outside this pool')
    if any(resources.get(k) != v for k, v in planned.items()):
        raise ValueError('Resource record belongs to different provider settings')


def unique(items):
    items = list(items)
    if len(items) > 1:
        raise UnresolvedLaunch('Multiple resources match one reserved identity')
    return items[0] if items else None


def wait_until(predicate, timeout, interval=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError('Cloud operation has not converged')
        time.sleep(interval)
