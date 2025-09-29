from threading import Lock


class AsyncSet:

    def __init__(self):
        self._set = set()
        self._lock = Lock()

    def add(self, item):
        with self._lock:
            self._set.add(item)

    def remove(self, item):
        with self._lock:
            self._set.remove(item)

    def discard(self, item):
        with self._lock:
            self._set.discard(item)

    def contains(self, item):
        with self._lock:
            return item in self._set

    def size(self):
        with self._lock:
            return len(self._set)

    def clear(self):
        with self._lock:
            self._set.clear()

    def to_list(self):
        with self._lock:
            return list(self._set)

    # Not thread-safe, use with caution
    def __len__(self):
        return len(self._set)

    def __sub__(self, other):
        with self._lock:
            return self._set - other._set
