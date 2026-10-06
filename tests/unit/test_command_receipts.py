import pytest

from limbowave.application.services.command_receipts import CommandReceipts, RuntimeConflict


def command(**overrides):
    return {"server_epoch": "epoch", "client_command_id": "id", "type": "send", **overrides}


def test_same_id_same_payload_deduplicates_and_copies():
    store = CommandReceipts("epoch")
    first, fresh = store.register("a", command())
    assert fresh and first["status"] == "pending"
    first["status"] = "tampered"
    store.finish("a", "id", status="accepted", run_id="run")
    assert store.register("a", command())[0]["run_id"] == "run"
    assert not store.register("a", command())[1]
    assert store.receipt("b", "id") is None
    assert store.register("b", command())[1]


def test_conflicts_epoch_and_capacity_do_not_evict():
    store = CommandReceipts("epoch", capacity=1)
    store.register("a", command())
    with pytest.raises(RuntimeConflict, match="command_id_reused"):
        store.register("a", command(text="different"))
    with pytest.raises(RuntimeConflict, match="epoch_mismatch"):
        store.register("a", command(server_epoch="old"))
    with pytest.raises(RuntimeConflict, match="receipt_capacity"):
        store.register("a", command(client_command_id="new"))
    assert store.receipt("a", "id")["status"] == "pending"
    store.revoke("a")
    assert store.register("a", command(client_command_id="new"))[1]


@pytest.mark.parametrize(
    "overrides", [{"client_command_id": ""}, {"n": float("nan")}, {"n": object()}]
)
def test_invalid_payload(overrides):
    with pytest.raises(RuntimeConflict):
        CommandReceipts("epoch").register("a", command(**overrides))
