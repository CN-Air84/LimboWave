"""Small LRU for disposable presentation objects, never authoritative data."""

from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping


class BoundedCache[K, V](MutableMapping[K, V]):
    def __init__(
        self, capacity: int = 128, *, max_weight: int | None = None,
        weight: Callable[[V], int] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if max_weight is not None and max_weight < 1:
            raise ValueError("max_weight must be positive")
        self.capacity = capacity
        self.max_weight = max_weight
        self._weight = weight or (lambda _value: 1)
        self._total_weight = 0
        self._values: OrderedDict[K, V] = OrderedDict()

    @property
    def total_weight(self) -> int:
        return self._total_weight

    def __getitem__(self, key: K) -> V:
        value = self._values[key]
        self._values.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        missing = object()
        previous = self._values.pop(key, missing)
        if previous is not missing:
            self._total_weight -= max(0, self._weight(previous))  # type: ignore[arg-type]
        self._values[key] = value
        self._values.move_to_end(key)
        self._total_weight += max(0, self._weight(value))
        while len(self._values) > self.capacity or (
            self.max_weight is not None and self._total_weight > self.max_weight
            and len(self._values) > 1
        ):
            _, evicted = self._values.popitem(last=False)
            self._total_weight -= max(0, self._weight(evicted))

    def __delitem__(self, key: K) -> None:
        value = self._values.pop(key)
        self._total_weight -= max(0, self._weight(value))

    def __iter__(self) -> Iterator[K]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)
