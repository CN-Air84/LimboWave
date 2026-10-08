import pytest

from limbowave.application.services.send_queue import SendQueue


def test_fifo_pins_scope_and_accepts_each_entry_once():
    queue = SendQueue()
    first = queue.enqueue(("a", "b1"), " first ")
    second = queue.enqueue(("a", "b2"), "second")
    third = queue.enqueue(("c", "b3"), "third")
    assert first.text == "first"
    assert queue.begin().id == first.id
    assert queue.begin() is None
    assert queue.cancel(first.id) is None
    assert not queue.accept(second.id)
    assert queue.accept(first.id)
    assert not queue.accept(first.id)
    assert queue.begin().scope == second.scope
    assert queue.accept(second.id)
    assert queue.begin().id == third.id


def test_failure_keeps_content_and_pauses_until_explicit_retry():
    queue = SendQueue()
    item = queue.enqueue(("a", "b"), "do not lose")
    queue.enqueue(("c", "d"), "next")
    queue.begin()
    queue.fail(item.id, "restore failed")
    assert queue.items[0].text == "do not lose"
    assert queue.items[0].state == "failed"
    assert not queue.ready
    assert queue.begin() is None
    queue.resume()
    assert queue.begin().id == item.id
    assert queue.accept(item.id)
    assert len(queue.items) == 1


def test_cancel_returns_text_and_does_not_unpause_remaining_requests():
    queue = SendQueue()
    first = queue.enqueue(("a", "b"), "first")
    queue.enqueue(("a", "b"), "second")
    queue.pause("stopped")
    assert queue.cancel(first.id).text == "first"
    assert queue.paused_reason == "stopped"
    assert not queue.ready
    queue.resume()
    assert queue.ready
    assert queue.cancel(first.id) is None
    queue.clear()
    assert not queue.items and not queue.paused_reason


def test_capacity_and_invalid_entries_never_replace_existing_items():
    queue = SendQueue()
    for text in ("", "  "):
        with pytest.raises(ValueError):
            queue.enqueue(("a", "b"), text)
    with pytest.raises(ValueError):
        queue.enqueue(("", "b"), "hello")
    for index in range(queue.MAX_ITEMS):
        queue.enqueue(("a", "b"), str(index))
    with pytest.raises(ValueError):
        queue.enqueue(("a", "b"), "overflow")
    assert len(queue.items) == queue.MAX_ITEMS
