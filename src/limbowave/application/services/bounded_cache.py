"""Small LRU for disposable presentation objects, never authoritative data."""

from collections import OrderedDict
from collections.abc import Iterator, MutableMapping


class BoundedCache[K, V](MutableMapping[K, V]):
    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._values: OrderedDict[K, V] = OrderedDict()

    def __getitem__(self, key: K) -> V:
        value = self._values[key]
        self._values.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self.capacity:
            self._values.popitem(last=False)

    def __delitem__(self, key: K) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)
