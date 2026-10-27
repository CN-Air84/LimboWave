from limbowave.application.services.bounded_cache import BoundedCache


def test_weight_budget_evicts_oldest_values() -> None:
    cache = BoundedCache[str, str](10, max_weight=5, weight=len)
    cache["a"] = "aaa"
    cache["b"] = "bb"
    assert list(cache) == ["a", "b"]
    cache["c"] = "cc"
    assert list(cache) == ["b", "c"]
    assert cache.total_weight == 4


def test_replacement_updates_weight_without_double_counting() -> None:
    cache = BoundedCache[str, str](3, max_weight=6, weight=len)
    cache["a"] = "aaaa"
    cache["a"] = "a"
    cache["b"] = "bbbb"
    assert list(cache) == ["a", "b"]
    assert cache.total_weight == 5
