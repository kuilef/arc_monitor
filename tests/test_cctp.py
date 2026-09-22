import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from arc_monitor.models import USDC, ZERO
from arc_monitor.storage import Store


T = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
TX = "0x" + "a" * 64
TX2 = "0x" + "b" * 64
BLOCK_HASH = "0x" + "c" * 64
RECIPIENT = "0x" + "1" * 40
FEE_RECIPIENT = "0x" + "6" * 40
DEPOSITOR = "0x" + "2" * 40
REMOTE_MESSENGER = "0x" + "00" * 12 + "3" * 40


def word(value):
    if isinstance(value, int):
        return value.to_bytes(32, "big")
    return bytes.fromhex(value.removeprefix("0x")).rjust(32, b"\0")


def dynamic(raw):
    return word(32) + word(len(raw)) + raw + b"\0" * (-len(raw) % 32)


def log(address, topic0, *, topics=(), data=b""):
    return {
        "address": address,
        "topics": [topic0, *topics],
        "data": "0x" + data.hex(),
        "removed": False,
        "blockNumber": "0x64",
        "blockHash": BLOCK_HASH,
        "transactionHash": TX,
        "logIndex": "0x1",
    }


def transfer(emitter, sender, recipient, amount):
    from arc_monitor.cctp import TRANSFER_TOPIC

    return log(emitter, TRANSFER_TOPIC, topics=("0x" + word(sender).hex(), "0x" + word(recipient).hex()),
               data=word(amount))


def burn_body(amount, recipient=RECIPIENT, fee=0, burn_token="0x" + "4" * 40):
    return b"".join([
        (1).to_bytes(4, "big"), word(burn_token), word(recipient), word(amount),
        word(DEPOSITOR), word(fee), word(fee), word(0),
    ])


def message(source, destination, nonce, body, *, sender=REMOTE_MESSENGER,
            recipient="0x" + "5" * 40, finality_executed=0):
    return b"".join([
        (1).to_bytes(4, "big"), source.to_bytes(4, "big"), destination.to_bytes(4, "big"),
        word(nonce), word(sender), word(recipient), word(0),
        (1000).to_bytes(4, "big"), finality_executed.to_bytes(4, "big"), body,
    ])


def incoming_logs(*, amount=1_000_000, fee=10_000, transfer_emitter=USDC, source=0, nonce=7,
                  include_fee_transfer=True):
    from arc_monitor.cctp import (MESSAGE_RECEIVED_TOPIC, MESSAGE_TRANSMITTER,
                                  MINT_AND_WITHDRAW_TOPIC, TOKEN_MESSENGER)

    body = burn_body(amount, fee=fee)
    received_data = word(source) + word(REMOTE_MESSENGER) + word(96) + word(len(body)) + body
    received_data += b"\0" * (-len(body) % 32)
    received = log(MESSAGE_TRANSMITTER, MESSAGE_RECEIVED_TOPIC,
                   topics=("0x" + word(DEPOSITOR).hex(), "0x" + word(nonce).hex(),
                           "0x" + word(2000).hex()), data=received_data)
    mint = log(TOKEN_MESSENGER, MINT_AND_WITHDRAW_TOPIC,
               topics=("0x" + word(RECIPIENT).hex(), "0x" + word(USDC).hex()),
               data=word(amount - fee) + word(fee))
    logs = [received, mint, transfer(transfer_emitter, ZERO, RECIPIENT, amount - fee)]
    if fee and include_fee_transfer:
        logs.append(transfer(USDC, ZERO, FEE_RECIPIENT, fee))
    return logs


def outgoing_logs(*, amount=2_000_000, destination=0, nonce=0):
    from arc_monitor.cctp import (DEPOSIT_FOR_BURN_TOPIC, MESSAGE_SENT_TOPIC,
                                  MESSAGE_TRANSMITTER, TOKEN_MESSENGER, TOKEN_MINTER)

    body = burn_body(amount, fee=0, burn_token=USDC)
    raw_message = message(26, destination, nonce, body, sender=TOKEN_MESSENGER,
                          recipient=REMOTE_MESSENGER)
    sent = log(MESSAGE_TRANSMITTER, MESSAGE_SENT_TOPIC, data=dynamic(raw_message))
    fields = (word(amount) + word(RECIPIENT) + word(destination) + word(REMOTE_MESSENGER)
              + word(0) + word(0) + word(224) + word(0))
    deposit = log(TOKEN_MESSENGER, DEPOSIT_FOR_BURN_TOPIC,
                  topics=("0x" + word(USDC).hex(), "0x" + word(DEPOSITOR).hex(),
                          "0x" + word(1000).hex()), data=fields)
    return [sent, deposit, transfer(USDC, TOKEN_MINTER, ZERO, amount)]


def discovery(tx=TX, *, emitter=None, topic=None):
    from arc_monitor.cctp import MESSAGE_RECEIVED_TOPIC, MESSAGE_TRANSMITTER

    return {
        "Block": {"Number": 100, "Time": T.isoformat().replace("+00:00", "Z")},
        "Transaction": {"Hash": tx},
        "LogHeader": {"Address": emitter or MESSAGE_TRANSMITTER, "Index": 1},
        "Topics": [{"Hash": topic or MESSAGE_RECEIVED_TOPIC}],
    }


class Provider:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute(self, query, root, cube):
        self.queries.append((query, root, cube))
        return self.rows


class RPC:
    def __init__(self, receipts):
        self.receipts = receipts
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or []))
        if method == "eth_chainId":
            return hex(5042)
        if method == "eth_getTransactionReceipt":
            return self.receipts[params[0]]
        if method == "eth_getBlockByNumber":
            return {"number": "0x64", "hash": BLOCK_HASH, "timestamp": hex(int(T.timestamp()))}
        raise AssertionError(method)


def receipt(logs, tx=TX):
    rows = []
    for index, item in enumerate(logs):
        item = dict(item)
        item["transactionHash"] = tx
        item["logIndex"] = hex(index)
        rows.append(item)
    return {"status": "0x1", "transactionHash": tx, "blockNumber": "0x64",
            "blockHash": BLOCK_HASH, "logs": rows}


def raw_payloads(store):
    with store.engine.connect() as conn:
        return [json.loads(v) for v in conn.execute(select(store.raw.c.payload)).scalars()]


def test_incoming_fee_is_reconciled_to_mint_and_canonical_transfer():
    from arc_monitor.cctp import collect_cctp

    with Store(":memory:") as store:
        result = collect_cctp(store, Provider([discovery()]), RPC({TX: receipt(incoming_logs())}),
                              T, T + timedelta(seconds=1))
        flows = store.events("flow")
        assert result == {"requests": 1, "discovered": 1, "matched": 1, "inserted": 1,
                          "unmatched": 0, "complete": True}
        assert len(flows) == 1 and flows[0].direction == "in"
        assert flows[0].amount_usdc == Decimal("1")
        assert flows[0].message_key == "cctp-v2:0:" + "0" * 63 + "7"
        assert store.covered("flows", T, T + timedelta(seconds=1))
        assert any("receipt" in p for p in raw_payloads(store))
        proof = store.get_state(f"cctp:{flows[0].id}")
        assert proof["flow_id"] == flows[0].id and proof["transaction"] == TX
        assert proof["receipt_raw_id"] and proof["block_hash"] == BLOCK_HASH
        assert proof["log_indexes"] == {
            "message": 0, "protocol": 1, "recipient_mint": 2, "fee_mint": 3,
            "burn": None,
        }
        assert proof["amounts_base_units"] == {
            "gross": "1000000", "recipient_net": "990000", "fee": "10000",
            "max_fee": "10000",
        }
        assert any(p.get("flow_id") == flows[0].id for p in raw_payloads(store))


def test_incoming_fee_requires_the_actual_fee_mint_transfer():
    from arc_monitor.cctp import collect_cctp

    logs = incoming_logs(include_fee_transfer=False)
    with Store(":memory:") as store:
        result = collect_cctp(store, Provider([discovery()]), RPC({TX: receipt(logs)}),
                              T, T + timedelta(seconds=1))
        assert result["matched"] == 0 and result["unmatched"] == 1
        assert store.events("flow") == [] and not store.covered("flows", T, T + timedelta(seconds=1))
        assert any("fee mint Transfer" in p.get("reason", "") for p in raw_payloads(store))


def test_bitquery_bare_topic_hash_is_normalized_before_cctp_matching():
    from arc_monitor.cctp import collect_cctp

    row = discovery()
    row["Topics"][0]["Hash"] = row["Topics"][0]["Hash"].removeprefix("0x")
    with Store(":memory:") as store:
        result = collect_cctp(
            store, Provider([row]), RPC({TX: receipt(incoming_logs())}),
            T, T + timedelta(seconds=1),
        )

        assert result["matched"] == 1 and result["complete"] is True
        assert len(store.events("flow")) == 1
        assert store.covered("flows", T, T + timedelta(seconds=1))


def test_outgoing_requires_arc_domain_message_deposit_and_real_burn():
    from arc_monitor.cctp import MESSAGE_SENT_TOPIC, MESSAGE_TRANSMITTER, collect_cctp

    row = discovery(emitter=MESSAGE_TRANSMITTER, topic=MESSAGE_SENT_TOPIC)
    with Store(":memory:") as store:
        result = collect_cctp(store, Provider([row]), RPC({TX: receipt(outgoing_logs())}),
                              T, T + timedelta(seconds=1))
        flow = store.events("flow")[0]
        assert result["complete"] is True and flow.direction == "out"
        assert flow.amount_usdc == Decimal("2")
        assert flow.message_key == f"cctp-v2-pending:26:{TX}:0"
        assert len("ops:cctp:" + flow.id) <= 80
        proof = store.get_state(f"cctp:{flow.id}")
        assert proof["transaction"] == TX and proof["log_indexes"]["burn"] == 2
        assert proof["identity_scope"] == "source_receipt_pending_iris_nonce"
        assert proof["amounts_base_units"]["recipient_net"] is None
        assert proof["amounts_base_units"]["fee"] is None


def test_outgoing_zero_nonce_identity_is_scoped_by_source_transaction_and_log():
    from arc_monitor.cctp import MESSAGE_SENT_TOPIC, MESSAGE_TRANSMITTER, collect_cctp

    rows = [discovery(TX, emitter=MESSAGE_TRANSMITTER, topic=MESSAGE_SENT_TOPIC),
            discovery(TX2, emitter=MESSAGE_TRANSMITTER, topic=MESSAGE_SENT_TOPIC)]
    rpc = RPC({TX: receipt(outgoing_logs(), TX), TX2: receipt(outgoing_logs(), TX2)})
    with Store(":memory:") as store:
        result = collect_cctp(store, Provider(rows), rpc, T, T + timedelta(seconds=1))
        flows = store.events("flow")
        assert result["matched"] == 2 and result["complete"] is True
        assert len({flow.id for flow in flows}) == 2
        assert {flow.message_key for flow in flows} == {
            f"cctp-v2-pending:26:{TX}:0", f"cctp-v2-pending:26:{TX2}:0",
        }


def test_fake_usdc_transfer_and_wrong_domain_are_unmatched_without_coverage():
    from arc_monitor.cctp import MESSAGE_SENT_TOPIC, MESSAGE_TRANSMITTER, collect_cctp

    fake_message = incoming_logs()
    fake_message[0] = dict(fake_message[0], address="0x" + "8" * 40)
    cases = [incoming_logs(transfer_emitter="0x" + "9" * 40), outgoing_logs(destination=26),
             outgoing_logs(nonce=9), fake_message]
    topics = [None, MESSAGE_SENT_TOPIC, MESSAGE_SENT_TOPIC, None]
    for logs, topic in zip(cases, topics, strict=True):
        row = discovery(emitter=MESSAGE_TRANSMITTER, topic=topic) if topic else discovery()
        with Store(":memory:") as store:
            result = collect_cctp(store, Provider([row]), RPC({TX: receipt(logs)}),
                                  T, T + timedelta(seconds=1))
            assert result["matched"] == 0 and result["unmatched"] == 1
            assert not store.covered("flows", T, T + timedelta(seconds=1))
            assert store.events("flow") == []
            assert any(p.get("reason") for p in raw_payloads(store))


def test_replay_nonce_and_multiple_messages_are_rejected_without_false_completeness():
    from arc_monitor.cctp import MESSAGE_RECEIVED_TOPIC, MESSAGE_TRANSMITTER, collect_cctp

    second = incoming_logs(nonce=7)
    second[0] = dict(second[0], transactionHash=TX2)
    rows = [discovery(), discovery(TX2)]
    rpc = RPC({TX: receipt(incoming_logs()), TX2: receipt(second, TX2)})
    with Store(":memory:") as store:
        result = collect_cctp(store, Provider(rows), rpc, T, T + timedelta(seconds=1))
        assert result["complete"] is False and result["unmatched"] == 1
        assert len(store.events("flow")) == 1
        assert not store.covered("flows", T, T + timedelta(seconds=1))

    ambiguous = incoming_logs()
    ambiguous.append(log(MESSAGE_TRANSMITTER, MESSAGE_RECEIVED_TOPIC,
                         topics=("0x" + word(DEPOSITOR).hex(), "0x" + word(8).hex(),
                                 "0x" + word(2000).hex()), data=ambiguous[0]["data"] and
                         bytes.fromhex(ambiguous[0]["data"][2:])))
    with Store(":memory:") as store:
        result = collect_cctp(store, Provider([discovery()]), RPC({TX: receipt(ambiguous)}),
                              T, T + timedelta(seconds=1))
        assert result["matched"] == 0 and result["complete"] is False


def test_saturated_one_second_discovery_never_claims_coverage_or_calls_rpc():
    from arc_monitor.cctp import collect_cctp

    provider = Provider([discovery(), discovery()])
    rpc = RPC({})
    with Store(":memory:") as store:
        result = collect_cctp(store, provider, rpc, T, T + timedelta(seconds=1), limit=2)
        assert result["requests"] == 1 and result["complete"] is False
        assert result["matched"] == 0 and not store.covered("flows", T, T + timedelta(seconds=1))
        assert not any(method == "eth_getTransactionReceipt" for method, _ in rpc.calls)


def test_empty_complete_window_is_deliberately_covered_and_query_is_scoped():
    from arc_monitor.cctp import MESSAGE_TRANSMITTER, TOKEN_MESSENGER, collect_cctp

    provider = Provider([])
    with Store(":memory:") as store:
        result = collect_cctp(store, provider, RPC({}), T, T + timedelta(seconds=1), limit=10)
        assert result["complete"] is True and result["discovered"] == 0
        assert store.covered("flows", T, T + timedelta(seconds=1))
    query, root, cube = provider.queries[0]
    assert root == "EVM" and cube == "Events"
    assert MESSAGE_TRANSMITTER in query.lower() and TOKEN_MESSENGER in query.lower()
