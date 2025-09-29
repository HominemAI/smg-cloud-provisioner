# AI Notice: This code is about 4% AI - AI was used to convert this from using a Set as a DS to using dict
from threading import Lock


class AsyncSet:

    def __init__(self):
        self._data = {}  # uid -> object mapping
        self._lock = Lock()

    def add(self, item):
        with self._lock:
            self._data[item.metadata.uid] = item

    def remove(self, item):
        with self._lock:
            del self._data[item.metadata.uid]

    def discard(self, item):
        with self._lock:
            self._data.pop(item.metadata.uid, None)

    def contains(self, item):
        with self._lock:
            return item.metadata.uid in self._data

    def __contains__(self, item):
        with self._lock:
            return item.metadata.uid in self._data

    def size(self):
        with self._lock:
            return len(self._data)

    def clear(self):
        with self._lock:
            self._data.clear()

    def to_list(self):
        with self._lock:
            return list(self._data.values())

    # Not thread-safe, use with caution
    def __len__(self):
        return len(self._data)

    def __sub__(self, other):
        with self._lock:
            result = AsyncSet()

            if isinstance(other, AsyncSet):
                # Subtracting another AsyncSet
                with other._lock:
                    result_uids = set(self._data.keys()) - set(other._data.keys())
            else:
                # Subtracting a regular set/list of objects
                other_uids = {obj.metadata.uid for obj in other}
                result_uids = set(self._data.keys()) - other_uids

            # Copy remaining objects to result
            for uid in result_uids:
                result._data[uid] = self._data[uid]

            return result

