import threading
import unittest
from types import SimpleNamespace

from async_tools import AsyncSet


def item(uid, value):
    return SimpleNamespace(metadata=SimpleNamespace(uid=uid), value=value)


class AsyncSetTests(unittest.TestCase):
    def test_snapshot_does_not_deadlock_and_is_independent(self):
        values = AsyncSet()
        first = item('one', 1)
        values.add(first)
        result = []
        thread = threading.Thread(target=lambda: result.append(values.to_list()), daemon=True)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive(), 'to_list deadlocked')
        values.clear()
        self.assertEqual(result, [[first]])

    def test_same_uid_updates_and_discard_is_idempotent(self):
        values = AsyncSet()
        values.add(item('one', 1))
        updated = item('one', 2)
        values.add(updated)
        self.assertEqual(values.to_list(), [updated])
        self.assertEqual(values.size(), 1)
        values.discard(updated)
        values.discard(updated)
        self.assertEqual(values.to_list(), [])
