from arc_monitor.models import CHAIN_ID, USDC, Trade
from arc_monitor.storage import Store
from test_storage import A, T, W


POOL = "0x" + "9" * 40
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TX = "0x" + "b" * 64
BLOCK_HASH = "0x" + "c" * 64


def topic(address):
    return "0x" + "0" * 24 + address[2:]


def transfer_log(token, sender, receiver, amount, index):
    return {"address": token, "logIndex": hex(index), "removed": False,
            "transactionHash": TX, "blockHash": BLOCK_HASH, "blockNumber": "0x2a",
            "topics": [TRANSFER_TOPIC, topic(sender), topic(receiver)],
            "data": "0x" + format(amount, "064x")}


def v4_swap_log(manager, pool_id, index=2):
    data = "0x" + "".join(format(value, "064x") for value in (1, 2, 2**96, 100, 0, 3000))
    return {"address": manager, "logIndex": hex(index), "removed": False,
            "transactionHash": TX, "blockHash": BLOCK_HASH, "blockNumber": "0x2a",
            "topics": [V4_SWAP_TOPIC, pool_id, topic(W)], "data": data}


def trade(**updates):
    values = dict(id="trade-1", token=A, ts=T, source="bitquery", tx=TX,
                  wallet=W, side="buy", quantity="5", quote_amount="10", pool=POOL)
    values.update(updates)
    return Trade(**values)


class Rpc:
    def __init__(self, logs, status="0x1"):
        self.logs = logs
        self.status = status
        self.calls = []

    def verify(self):
        return {"chain_id": CHAIN_ID, "verified": True}

    def call(self, method, params=None):
        self.calls.append((method, params or []))
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": TX, "status": self.status,
                    "blockNumber": "0x2a", "blockHash": BLOCK_HASH,
                    "logs": self.logs}
        if method == "eth_getBlockByNumber":
            return {"number": "0x2a", "hash": "0x" + "c" * 64,
                    "timestamp": hex(int(T.timestamp()))}
        if method == "eth_call":
            decimals = 6 if params[0]["to"].lower() == USDC else 18
            return "0x" + format(decimals, "064x")
        raise AssertionError((method, params))


def matching_logs():
    return [
        transfer_log(A, POOL, W, 5 * 10**18, 3),
        transfer_log(USDC, W, POOL, 10 * 10**6, 4),
    ]


def test_exact_receipt_match_preserves_trade_and_records_integer_legs():
    from arc_monitor.reconciliation import reconcile_trade

    original = trade()
    with Store(":memory:", mode="demo") as store:
        store.ingest([original])
        report = reconcile_trade(store, Rpc(matching_logs()), original)
        assert report["status"] == "matched"
        assert report["chain_verified"] is True
        assert report["original_trade"] == original.model_dump(mode="json")
        assert report["token_leg"]["raw_amount"] == str(5 * 10**18)
        assert report["quote_leg"]["raw_amount"] == str(10 * 10**6)
        assert report["token_leg"]["log_index"] == 3 and report["quote_leg"]["log_index"] == 4
        assert report["provider_conversion_residual_raw_units"] == {"token": "0", "quote": "0"}
        assert report["raw_amount_difference"] == {"token": "0", "quote": "0"}
        assert store.count("trade") == 1
        assert store.get_state("reconciliation:trade-1")["status"] == "matched"
        assert report["receipt_raw_id"]


def test_multiple_candidate_legs_remain_unknown():
    from arc_monitor.reconciliation import reconcile_trade

    logs = matching_logs() + [transfer_log(A, POOL, W, 5 * 10**18, 5)]
    with Store(":memory:", mode="demo") as store:
        report = reconcile_trade(store, Rpc(logs), trade())
        assert report["status"] == "unknown"
        assert report["reason"] == "ambiguous_transfer_legs"
        assert report["candidate_counts"] == {"token": 2, "quote": 1}


def test_raw_transfer_mismatch_is_distinct_from_provider_conversion_residual():
    from arc_monitor.reconciliation import reconcile_trade

    logs = matching_logs()
    logs[1]["data"] = "0x" + format(10 * 10**6 + 10, "064x")
    with Store(":memory:", mode="demo") as store:
        report = reconcile_trade(store, Rpc(logs), trade())

    assert report["status"] == "mismatch"
    assert report["reason"] == "amount_mismatch"
    assert report["provider_conversion_residual_raw_units"] == {"token": "0", "quote": "0"}
    assert report["raw_amount_difference"] == {"token": "0", "quote": "10"}


def test_failed_receipt_is_saved_but_never_matched():
    from arc_monitor.reconciliation import reconcile_trade

    with Store(":memory:", mode="demo") as store:
        report = reconcile_trade(store, Rpc(matching_logs(), status="0x0"), trade())
        assert report["status"] == "unknown" and report["reason"] == "transaction_failed"
        assert report["receipt_raw_id"]


def test_provider_trade_time_must_match_verified_receipt_block_time():
    from arc_monitor.reconciliation import reconcile_trade

    class WrongTimeRpc(Rpc):
        def call(self, method, params=None):
            result = super().call(method, params)
            if method == "eth_getBlockByNumber":
                return {**result, "timestamp": hex(int(T.timestamp()) + 1)}
            return result

    with Store(":memory:", mode="demo") as store:
        report = reconcile_trade(store, WrongTimeRpc(matching_logs()), trade())
        assert report["status"] == "unknown"
        assert report["reason"] == "block_time_mismatch"


def test_removed_receipt_log_cannot_support_reconciliation():
    from arc_monitor.reconciliation import reconcile_trade

    logs = matching_logs()
    logs[0]["removed"] = True
    with Store(":memory:", mode="demo") as store:
        report = reconcile_trade(store, Rpc(logs), trade())

    assert report["status"] == "unknown"
    assert report["reason"] == "removed_receipt_log"


def test_receipt_log_must_belong_to_the_verified_receipt():
    from arc_monitor.reconciliation import reconcile_trade

    wrong_identity = {
        "transactionHash": "0x" + "d" * 64,
        "blockHash": "0x" + "e" * 64,
        "blockNumber": "0x2b",
    }
    for field, value in wrong_identity.items():
        logs = matching_logs()
        logs[0][field] = value
        with Store(":memory:", mode="demo") as store:
            report = reconcile_trade(store, Rpc(logs), trade())

        assert report["status"] == "unknown", field
        assert report["reason"] == "receipt_log_identity_mismatch", field


def test_v4_pool_context_comes_from_stored_raw_manager_and_topic():
    from arc_monitor.reconciliation import reconcile_trade

    pool_id = "0x" + "7" * 64
    manager = "0x" + "6" * 40
    init_tx = "0x" + "5" * 64
    data = "0x" + "".join(format(value, "064x") for value in (3000, 60, 0, 2**96, 0))
    with Store(":memory:", mode="demo") as store:
        raw_id = store.save_raw("bitquery:launches", {
            "rows": [{"Block": {"Number": "42"}, "Transaction": {"Hash": init_tx},
                      "LogHeader": {"Address": manager, "Index": 7, "Data": data,
                                    "Removed": False},
                      "Topics": [{"Hash": V4_INITIALIZE_TOPIC}, {"Hash": pool_id},
                                 {"Hash": topic(A)}, {"Hash": topic(USDC)}]}]})
        item = trade(pool=pool_id)
        report = reconcile_trade(store, Rpc([v4_swap_log(manager, pool_id)]), item)
        assert report["status"] == "unknown"
        assert report["pool_context"] == {"kind": "uniswap_v4", "pool_id": pool_id,
                                          "manager": manager, "raw_id": raw_id,
                                          "event": "Initialize",
                                          "event_topic": V4_INITIALIZE_TOPIC,
                                          "transaction": init_tx,
                                          "block_number": 42,
                                          "provider_log_index": 7,
                                          "swap_event_topic": V4_SWAP_TOPIC,
                                          "swap_log_index": 2}
        assert report["reason"] == "ambiguous_transfer_legs"


def test_v4_initialize_without_same_manager_swap_stays_unresolved():
    from arc_monitor.reconciliation import reconcile_trade

    pool_id = "0x" + "7" * 64
    manager = "0x" + "6" * 40
    data = "0x" + "".join(format(value, "064x") for value in (3000, 60, 0, 2**96, 0))
    with Store(":memory:", mode="demo") as store:
        store.save_raw("bitquery:launches", {
            "rows": [{"Block": {"Number": "42"},
                      "Transaction": {"Hash": "0x" + "5" * 64},
                      "LogHeader": {"Address": manager, "Index": 7, "Data": data,
                                    "Removed": False},
                      "Topics": [{"Hash": V4_INITIALIZE_TOPIC}, {"Hash": pool_id},
                                 {"Hash": topic(A)}, {"Hash": topic(USDC)}]}]})
        report = reconcile_trade(store, Rpc([]), trade(pool=pool_id))

    assert report["status"] == "unknown"
    assert report["pool_context"]["manager"] is None
    assert report["reason"] == "pool_identity_unresolved"


def test_v4_pool_context_rejects_generic_topic_match_without_initialize_evidence():
    from arc_monitor.reconciliation import reconcile_trade

    pool_id = "0x" + "7" * 64
    manager = "0x" + "6" * 40
    with Store(":memory:", mode="demo") as store:
        store.save_raw("bitquery:launches", {
            "rows": [{"Block": {"Number": "42"}, "Transaction": {"Hash": "0x" + "5" * 64},
                      "LogHeader": {"Address": manager, "Index": 7, "Data": "0x"},
                      "Topics": [{"Hash": "0x" + "1" * 64}, {"Hash": pool_id}]}]})
        report = reconcile_trade(store, Rpc([]), trade(pool=pool_id))

    assert report["status"] == "unknown"
    assert report["pool_context"]["manager"] is None
    assert report["reason"] == "pool_identity_unresolved"
