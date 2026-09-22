"""Strict, bounded CCTP V2 reconciliation for Arc mainnet.

Bitquery is used only to discover candidate transactions. A Flow is accepted from
the canonical Arc receipt after all message, protocol-event, and USDC Transfer
fields agree. Unknown or ambiguous transactions remain raw evidence and prevent
coverage from being asserted for the requested interval.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .models import CHAIN_ID, USDC, Flow, address, iso, units, utc
from .providers.bitquery import topic_hex

ARC_DOMAIN = 26
TOKEN_MESSENGER = "0x28b5a0e9c621a5badaa536219b3a228c8168cf5d"
MESSAGE_TRANSMITTER = "0x81d40f21f12a8f0e3252bccb954d722d4c464b64"
TOKEN_MINTER = "0xfd78ee919681417d192449715b2594ab58f5d002"
ZERO_ADDRESS = "0x" + "0" * 40

# keccak256 of the exact Solidity event declarations in Circle's CCTP V2 contracts.
MESSAGE_SENT_TOPIC = "0x8c5261668696ce22758910d05bab8f186d6eb247ceac2af2e82c7dc17669b036"
MESSAGE_RECEIVED_TOPIC = "0xff48c13eda96b1cceacc6b9edeedc9e9db9d6226afbc30146b720c19d3addb1c"
DEPOSIT_FOR_BURN_TOPIC = "0x0c8c1cbdc5190613ebd485511d4e2812cfa45eecb79d845893331fedad5130a5"
MINT_AND_WITHDRAW_TOPIC = "0x50c55e915134d457debfa58eb6f4342956f8b0616d51a89a3659360178e1ab63"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_TX = re.compile(r"0x[0-9a-fA-F]{64}")
_TOPIC = re.compile(r"0x[0-9a-fA-F]{64}")


class CctpMismatch(ValueError):
    """A candidate receipt cannot be reconciled without inference."""


def _graphql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cctp_query(start: datetime, end: datetime, limit: int) -> str:
    """Build the narrow Arc Events discovery query for canonical CCTP emitters."""
    start, end = utc(start), utc(end)
    if start >= end or start.microsecond or end.microsecond or not 1 <= limit <= 10_000:
        raise ValueError("invalid CCTP discovery range or limit")
    since = _graphql_string(iso(start))
    till = _graphql_string(iso(end - timedelta(seconds=1)))
    sent, received, deposit, mint = map(_graphql_string, (
        MESSAGE_SENT_TOPIC, MESSAGE_RECEIVED_TOPIC, DEPOSIT_FOR_BURN_TOPIC, MINT_AND_WITHDRAW_TOPIC,
    ))
    transmitter, messenger = map(_graphql_string, (MESSAGE_TRANSMITTER, TOKEN_MESSENGER))
    # TokenMessenger is included as a known Arc CCTP address even though discovery
    # uses MessageSent/MessageReceived; receipt reconciliation requires both contracts.
    return f"""query {{ EVM(network: arc) {{ Events(
      limit: {{count: {limit}}}, orderBy: {{ascending: Block_Time}},
      where: {{Block: {{Time: {{since: {since}, till: {till}}}}},
        TransactionStatus: {{Success: true}}, Call: {{Success: true, Reverted: false}},
        LogHeader: {{Removed: false}}, any: [
          {{LogHeader: {{Address: {{is: {transmitter}}}}}, Topics: {{includes: [{{Hash: {{is: {sent}}}}}]}}}},
          {{LogHeader: {{Address: {{is: {transmitter}}}}}, Topics: {{includes: [{{Hash: {{is: {received}}}}}]}}}},
          {{LogHeader: {{Address: {{is: {messenger}}}}}, Topics: {{includes: [{{Hash: {{is: {deposit}}}}}]}}}},
          {{LogHeader: {{Address: {{is: {messenger}}}}}, Topics: {{includes: [{{Hash: {{is: {mint}}}}}]}}}}
        ]}}) {{
        Block {{Number Time}} Transaction {{Hash}} LogHeader {{Address Index}} Topics {{Hash}}
      }} }} }}"""


def _hex_bytes(value, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise CctpMismatch(f"malformed {label}")
    try:
        return bytes.fromhex(value[2:])
    except ValueError:
        raise CctpMismatch(f"malformed {label}") from None


def _topic(value, label: str = "topic") -> bytes:
    if not isinstance(value, str) or not _TOPIC.fullmatch(value):
        raise CctpMismatch(f"malformed {label}")
    return bytes.fromhex(value[2:])


def _bytes32_address(raw: bytes, label: str) -> str:
    if len(raw) != 32 or raw[:12] != b"\0" * 12:
        raise CctpMismatch(f"{label} is not a padded EVM address")
    return address("0x" + raw[12:].hex())


def _topic_address(value: str, label: str) -> str:
    return _bytes32_address(_topic(value, label), label)


def _uint(raw: bytes) -> int:
    if len(raw) != 32:
        raise CctpMismatch("invalid uint256 word")
    return int.from_bytes(raw, "big")


def _words(data: bytes, count: int) -> list[bytes]:
    if len(data) < count * 32:
        raise CctpMismatch("event data is shorter than its ABI")
    return [data[i * 32:(i + 1) * 32] for i in range(count)]


def _dynamic(data: bytes, offset_word: bytes, label: str) -> bytes:
    offset = _uint(offset_word)
    if offset % 32 or offset + 32 > len(data):
        raise CctpMismatch(f"invalid {label} offset")
    length = _uint(data[offset:offset + 32])
    end = offset + 32 + length
    padded_end = end + (-length % 32)
    if padded_end > len(data):
        raise CctpMismatch(f"truncated {label}")
    if any(data[end:padded_end]):
        raise CctpMismatch(f"nonzero {label} padding")
    return data[offset + 32:end]


def _log_parts(log: dict) -> tuple[str, list[str], bytes]:
    if not isinstance(log, dict) or log.get("removed") is True:
        raise CctpMismatch("removed or malformed receipt log")
    emitter = address(log.get("address"))
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        raise CctpMismatch("receipt log has no topics")
    for item in topics:
        _topic(item)
    return emitter, [item.lower() for item in topics], _hex_bytes(log.get("data"), "log data")


def _decode_burn_body(body: bytes) -> dict:
    if len(body) < 228:
        raise CctpMismatch("CCTP burn body is too short")
    return {
        "version": int.from_bytes(body[0:4], "big"),
        "burn_token": body[4:36],
        "mint_recipient": body[36:68],
        "amount": _uint(body[68:100]),
        "message_sender": body[100:132],
        "max_fee": _uint(body[132:164]),
        "fee": _uint(body[164:196]),
        "expiration_block": _uint(body[196:228]),
        "hook_data": body[228:],
    }


def _decode_message(raw: bytes) -> dict:
    if len(raw) < 148:
        raise CctpMismatch("CCTP message is too short")
    return {
        "version": int.from_bytes(raw[0:4], "big"),
        "source_domain": int.from_bytes(raw[4:8], "big"),
        "destination_domain": int.from_bytes(raw[8:12], "big"),
        "nonce": raw[12:44],
        "sender": raw[44:76],
        "recipient": raw[76:108],
        "destination_caller": raw[108:140],
        "min_finality": int.from_bytes(raw[140:144], "big"),
        "finality_executed": int.from_bytes(raw[144:148], "big"),
        "body": _decode_burn_body(raw[148:]),
    }


def _message_sent(log: dict) -> dict:
    emitter, topics, data = _log_parts(log)
    if emitter != MESSAGE_TRANSMITTER or topics != [MESSAGE_SENT_TOPIC]:
        raise CctpMismatch("MessageSent emitter or topics mismatch")
    words = _words(data, 1)
    return _decode_message(_dynamic(data, words[0], "MessageSent message"))


def _message_received(log: dict) -> dict:
    emitter, topics, data = _log_parts(log)
    if emitter != MESSAGE_TRANSMITTER or len(topics) != 4 or topics[0] != MESSAGE_RECEIVED_TOPIC:
        raise CctpMismatch("MessageReceived emitter or topics mismatch")
    words = _words(data, 3)
    body = _dynamic(data, words[2], "MessageReceived body")
    return {
        "caller": _topic_address(topics[1], "MessageReceived caller"),
        "source_domain": _uint(words[0]),
        "nonce": _topic(topics[2], "MessageReceived nonce"),
        "sender": words[1],
        "finality_executed": int.from_bytes(_topic(topics[3], "MessageReceived finality"), "big"),
        "body": _decode_burn_body(body),
    }


def _deposit(log: dict) -> dict:
    emitter, topics, data = _log_parts(log)
    if emitter != TOKEN_MESSENGER or len(topics) != 4 or topics[0] != DEPOSIT_FOR_BURN_TOPIC:
        raise CctpMismatch("DepositForBurn emitter or topics mismatch")
    words = _words(data, 7)
    _dynamic(data, words[6], "DepositForBurn hook data")
    return {
        "burn_token": _topic_address(topics[1], "burn token"),
        "depositor": _topic_address(topics[2], "depositor"),
        "min_finality": int.from_bytes(_topic(topics[3], "minimum finality"), "big"),
        "amount": _uint(words[0]),
        "mint_recipient": words[1],
        "destination_domain": _uint(words[2]),
        "destination_messenger": words[3],
        "destination_caller": words[4],
        "max_fee": _uint(words[5]),
    }


def _mint(log: dict) -> dict:
    emitter, topics, data = _log_parts(log)
    if emitter != TOKEN_MESSENGER or len(topics) != 3 or topics[0] != MINT_AND_WITHDRAW_TOPIC:
        raise CctpMismatch("MintAndWithdraw emitter or topics mismatch")
    words = _words(data, 2)
    return {"recipient": _topic_address(topics[1], "mint recipient"),
            "token": _topic_address(topics[2], "mint token"),
            "amount": _uint(words[0]), "fee": _uint(words[1])}


def _transfers(logs: list[dict]) -> list[dict]:
    found = []
    for item in logs:
        try:
            emitter, topics, data = _log_parts(item)
        except (CctpMismatch, ValueError, TypeError):
            continue
        if emitter != USDC or len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
            continue
        words = _words(data, 1)
        found.append({"from": _topic_address(topics[1], "transfer sender"),
                      "to": _topic_address(topics[2], "transfer recipient"),
                      "amount": _uint(words[0]), "log_index": _log_index(item)})
    return found


def _log_index(log: dict) -> int:
    value = log.get("logIndex")
    try:
        index = int(value, 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise CctpMismatch("receipt log index is malformed") from None
    if isinstance(value, bool) or index < 0:
        raise CctpMismatch("receipt log index is malformed")
    return index


def _one(logs: list[dict], emitter: str, topic0: str, label: str) -> dict:
    selected = []
    for item in logs:
        try:
            address_value, topics, _ = _log_parts(item)
        except (CctpMismatch, ValueError, TypeError):
            continue
        if address_value == emitter and topics[0] == topic0:
            selected.append(item)
    if len(selected) != 1:
        raise CctpMismatch(f"expected exactly one {label}, found {len(selected)}")
    return selected[0]


def _message_key(source_domain: int, nonce: bytes) -> str:
    return f"cctp-v2:{source_domain}:{nonce.hex()}"


def _pending_message_key(source_domain: int, transaction: str, log_index: int) -> str:
    return f"cctp-v2-pending:{source_domain}:{transaction}:{log_index}"


def _flow_id(direction: str, message_key: str) -> str:
    # Keep ops:cctp:<FlowID> within the Store settings key's 80-character schema.
    digest = hashlib.sha256(f"{direction}:{message_key}".encode()).hexdigest()[:48]
    return f"cctp-flow:{digest}"


def _reconcile_receipt(receipt: dict, block: dict, expected_topics: set[str],
                       expected_times: set[datetime], start: datetime,
                       end: datetime) -> tuple[Flow, dict]:
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        raise CctpMismatch("transaction receipt is absent or unsuccessful")
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise CctpMismatch("receipt logs are not a list")
    receipt_tx = str(receipt.get("transactionHash", "")).lower()
    receipt_hash = str(receipt.get("blockHash", "")).lower()
    receipt_number = str(receipt.get("blockNumber", "")).lower()
    log_indexes = []
    for item in logs:
        if not isinstance(item, dict):
            raise CctpMismatch("receipt contains a malformed log")
        if (str(item.get("transactionHash", "")).lower() != receipt_tx
                or str(item.get("blockHash", "")).lower() != receipt_hash
                or str(item.get("blockNumber", "")).lower() != receipt_number):
            raise CctpMismatch("receipt log identity does not match its transaction and block")
        log_indexes.append(_log_index(item))
    if len(set(log_indexes)) != len(log_indexes):
        raise CctpMismatch("receipt contains duplicate log indexes")
    message_logs = []
    for item in logs:
        try:
            emitter, topics, _ = _log_parts(item)
        except (CctpMismatch, ValueError, TypeError):
            continue
        if emitter == MESSAGE_TRANSMITTER and topics[0] in {MESSAGE_SENT_TOPIC, MESSAGE_RECEIVED_TOPIC}:
            message_logs.append((topics[0], item))
    if len(message_logs) != 1:
        raise CctpMismatch(f"expected one canonical CCTP message event, found {len(message_logs)}")
    event_topic, message_log = message_logs[0]
    if expected_topics != {event_topic}:
        raise CctpMismatch("Bitquery discovery and canonical receipt direction mismatch")
    if not isinstance(block, dict) or block.get("hash", "").lower() != str(receipt.get("blockHash", "")).lower():
        raise CctpMismatch("receipt block hash does not match canonical block")
    if str(block.get("number", receipt.get("blockNumber", ""))).lower() != str(receipt.get("blockNumber", "")).lower():
        raise CctpMismatch("receipt block number does not match canonical block")
    try:
        ts = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        raise CctpMismatch("canonical block timestamp is malformed") from None
    if not start <= ts < end:
        raise CctpMismatch("canonical transaction timestamp is outside discovery window")
    if expected_times != {ts}:
        raise CctpMismatch("RPC block time and Bitquery discovery time mismatch")
    transfers = _transfers(logs)

    if event_topic == MESSAGE_RECEIVED_TOPIC:
        received = _message_received(message_log)
        protocol_log = _one(logs, TOKEN_MESSENGER, MINT_AND_WITHDRAW_TOPIC, "MintAndWithdraw")
        mint = _mint(protocol_log)
        body = received["body"]
        if received["source_domain"] == ARC_DOMAIN:
            raise CctpMismatch("incoming message claims Arc as its source domain")
        if received["nonce"] == b"\0" * 32:
            raise CctpMismatch("incoming message has the reserved zero nonce")
        if body["version"] != 1 or body["amount"] <= body["fee"] or body["fee"] > body["max_fee"]:
            raise CctpMismatch("incoming burn body version, amount, or fee is invalid")
        recipient = _bytes32_address(body["mint_recipient"], "burn-body mint recipient")
        net = body["amount"] - body["fee"]
        if mint != {"recipient": recipient, "token": USDC, "amount": net, "fee": body["fee"]}:
            raise CctpMismatch("MintAndWithdraw does not match burn body amount less fee")
        recipient_mints = [t for t in transfers if t["from"] == ZERO_ADDRESS
                           and t["to"] == recipient and t["amount"] == net]
        if len(recipient_mints) != 1:
            raise CctpMismatch("canonical USDC mint Transfer does not match MintAndWithdraw")
        fee_mint = None
        if body["fee"]:
            fee_mints = [t for t in transfers if t["from"] == ZERO_ADDRESS
                         and t["amount"] == body["fee"]
                         and t["log_index"] != recipient_mints[0]["log_index"]]
            if len(fee_mints) != 1:
                raise CctpMismatch("canonical USDC fee mint Transfer is missing or ambiguous")
            fee_mint = fee_mints[0]
        direction, amount = "in", body["amount"]
        source_domain, nonce = received["source_domain"], received["nonce"]
        proof = {
            "log_indexes": {"message": _log_index(message_log),
                            "protocol": _log_index(protocol_log),
                            "recipient_mint": recipient_mints[0]["log_index"],
                            "fee_mint": fee_mint["log_index"] if fee_mint else None,
                            "burn": None},
            "amounts_base_units": {"gross": str(body["amount"]),
                                   "recipient_net": str(net), "fee": str(body["fee"]),
                                   "max_fee": str(body["max_fee"])},
            "mint_recipient": recipient,
            "fee_recipient": fee_mint["to"] if fee_mint else None,
            "identity_scope": "final_source_domain_nonce",
        }
    else:
        sent = _message_sent(message_log)
        protocol_log = _one(logs, TOKEN_MESSENGER, DEPOSIT_FOR_BURN_TOPIC, "DepositForBurn")
        deposit = _deposit(protocol_log)
        body = sent["body"]
        if (sent["version"] != 1 or sent["source_domain"] != ARC_DOMAIN
                or sent["destination_domain"] == ARC_DOMAIN):
            raise CctpMismatch("outgoing message domain or version is invalid")
        if sent["nonce"] != b"\0" * 32 or sent["finality_executed"] != 0:
            raise CctpMismatch("outgoing MessageSent must contain Circle's zero nonce/finality placeholders")
        checks = [
            _bytes32_address(sent["sender"], "message sender") == TOKEN_MESSENGER,
            sent["recipient"] == deposit["destination_messenger"],
            sent["destination_caller"] == deposit["destination_caller"],
            sent["destination_domain"] == deposit["destination_domain"],
            sent["min_finality"] == deposit["min_finality"],
            body["version"] == 1,
            _bytes32_address(body["burn_token"], "burn-body token") == USDC,
            body["mint_recipient"] == deposit["mint_recipient"],
            body["amount"] == deposit["amount"],
            _bytes32_address(body["message_sender"], "burn-body sender") == deposit["depositor"],
            body["max_fee"] == deposit["max_fee"],
            body["fee"] == 0 and body["expiration_block"] == 0,
            deposit["burn_token"] == USDC,
        ]
        if not all(checks):
            raise CctpMismatch("MessageSent, burn body, and DepositForBurn fields disagree")
        amount = deposit["amount"]
        burns = [t for t in transfers if t["from"] == TOKEN_MINTER
                 and t["to"] == ZERO_ADDRESS and t["amount"] == amount]
        if len(burns) != 1:
            raise CctpMismatch("canonical USDC burn Transfer does not match DepositForBurn")
        direction, source_domain, nonce = "out", ARC_DOMAIN, sent["nonce"]
        proof = {
            "log_indexes": {"message": _log_index(message_log),
                            "protocol": _log_index(protocol_log),
                            "recipient_mint": None, "fee_mint": None,
                            "burn": burns[0]["log_index"]},
            # The destination may execute a fee later. Do not derive a destination
            # recipient net or executed fee from the source-side maxFee.
            "amounts_base_units": {"gross": str(amount), "recipient_net": None,
                                   "fee": None, "max_fee": str(body["max_fee"])},
            "mint_recipient": "0x" + body["mint_recipient"].hex(),
            "fee_recipient": None,
            "identity_scope": "source_receipt_pending_iris_nonce",
        }
    if amount <= 0:
        raise CctpMismatch("reconciled CCTP amount is zero")
    key = (_pending_message_key(source_domain, receipt_tx, _log_index(message_log))
           if direction == "out" else _message_key(source_domain, nonce))
    flow = Flow(id=_flow_id(direction, key), ts=ts, source="cctp_rpc",
                direction=direction, amount_usdc=units(amount, 6), message_key=key)
    proof.update({"flow_id": flow.id, "message_key": key, "direction": direction,
                  "transaction": receipt_tx, "block_hash": receipt_hash,
                  "block_number": int(receipt_number, 16), "timestamp": iso(ts),
                  "amounts_usdc": {
                      key: (str(units(int(value), 6)) if value is not None else None)
                      for key, value in proof["amounts_base_units"].items()
                  }})
    return flow, proof


def _discovery_identity(row: dict) -> tuple[str, str, int, datetime]:
    try:
        tx = row["Transaction"]["Hash"].lower()
        emitter = address(row["LogHeader"]["Address"])
        block_number = int(row["Block"]["Number"])
        ts = datetime.fromisoformat(row["Block"]["Time"].replace("Z", "+00:00")).astimezone(timezone.utc)
        topics = row["Topics"]
        topic0 = "0x" + topic_hex(topics[0]["Hash"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise CctpMismatch("malformed Bitquery discovery row") from None
    if not _TX.fullmatch(tx):
        raise CctpMismatch("discovery row transaction or emitter is invalid")
    expected = {
        (MESSAGE_TRANSMITTER, MESSAGE_SENT_TOPIC): MESSAGE_SENT_TOPIC,
        (MESSAGE_TRANSMITTER, MESSAGE_RECEIVED_TOPIC): MESSAGE_RECEIVED_TOPIC,
        (TOKEN_MESSENGER, DEPOSIT_FOR_BURN_TOPIC): MESSAGE_SENT_TOPIC,
        (TOKEN_MESSENGER, MINT_AND_WITHDRAW_TOPIC): MESSAGE_RECEIVED_TOPIC,
    }.get((emitter, topic0))
    if expected is None:
        raise CctpMismatch("discovery row is not a CCTP message event")
    return tx, expected, block_number, ts


def collect_cctp(store, provider, rpc, start: datetime, end: datetime, *, limit: int = 100,
                 max_transactions: int = 20) -> dict:
    """Collect fully reconciled Arc CCTP flows for a half-open UTC interval.

    A non-complete result intentionally leaves ``flows`` coverage absent. Matched
    flows remain useful facts even when another candidate in the window is unknown.
    """
    start, end = utc(start), utc(end)
    if start >= end or start.microsecond or end.microsecond or not 1 <= limit <= 10_000:
        raise ValueError("invalid CCTP collection range or limit")
    if not isinstance(max_transactions, int) or isinstance(max_transactions, bool) or max_transactions < 1:
        raise ValueError("max_transactions must be a positive integer")
    if end > datetime.now(timezone.utc):
        raise ValueError("cannot collect or assert CCTP coverage for a future interval")
    result = {"requests": 0, "discovered": 0, "matched": 0, "inserted": 0,
              "unmatched": 0, "complete": True}
    rows: list[dict] = []

    def discover(left: datetime, right: datetime) -> None:
        query = cctp_query(left, right, limit)
        batch = provider.execute(query, "EVM", "Events")
        result["requests"] += 1
        store.save_raw("cctp_bitquery", {"start": iso(left), "end": iso(right), "rows": batch})
        if not isinstance(batch, list):
            raise CctpMismatch("Bitquery CCTP discovery rows are not a list")
        if len(batch) >= limit:
            seconds = int((right - left).total_seconds())
            if seconds <= 1:
                result["complete"] = False
                store.save_raw("cctp_unmatched", {"start": iso(left), "end": iso(right),
                               "reason": "saturated_one_second_window", "rows": batch})
                return
            middle = left + timedelta(seconds=seconds // 2)
            discover(left, middle)
            discover(middle, right)
            return
        rows.extend(batch)

    discover(start, end)
    if not result["complete"]:
        return result

    candidates: dict[str, dict] = {}
    for row in rows:
        try:
            tx, topic0, number, row_ts = _discovery_identity(row)
            if not start <= row_ts < end:
                raise CctpMismatch("discovery row timestamp is outside requested window")
            candidate = candidates.setdefault(tx, {"rows": [], "topics": set(), "blocks": set(),
                                                   "times": set()})
            candidate["rows"].append(row)
            candidate["topics"].add(topic0)
            candidate["blocks"].add(number)
            candidate["times"].add(row_ts)
        except (CctpMismatch, ValueError, TypeError) as exc:
            result["unmatched"] += 1
            result["complete"] = False
            store.save_raw("cctp_unmatched", {"reason": str(exc), "discovery": row})
    result["discovered"] = len(candidates) + result["unmatched"]
    if len(candidates) > max_transactions:
        result["unmatched"] += len(candidates)
        result["complete"] = False
        store.save_raw("cctp_unmatched", {"reason": "max_transactions_exceeded",
                       "count": len(candidates), "limit": max_transactions, "transactions": sorted(candidates)})
        return result
    if not candidates and result["complete"]:
        result["inserted"] = store.record_window("flows", start, end, [], "bitquery+rpc")
        return result

    try:
        if int(rpc.call("eth_chainId"), 16) != CHAIN_ID:
            raise ValueError(f"RPC chain mismatch: expected {CHAIN_ID}")
    except (TypeError, ValueError):
        raise ValueError(f"RPC chain mismatch: expected {CHAIN_ID}") from None

    matched: list[Flow] = []
    accepted_proofs: list[dict] = []
    seen: dict[tuple[str, str], Flow] = {}
    seen_in_run: dict[tuple[str, str], str] = {}
    for item in store.events("flow"):
        seen[(item.direction, item.message_key)] = item
    for tx, candidate in sorted(candidates.items()):
        receipt = None
        receipt_raw_id = None
        try:
            if (len(candidate["blocks"]) != 1 or len(candidate["topics"]) != 1
                    or len(candidate["times"]) != 1):
                raise CctpMismatch("candidate has inconsistent discovery events")
            receipt = rpc.call("eth_getTransactionReceipt", [tx])
            receipt_raw_id = store.save_raw("cctp_rpc_receipt", {"transaction": tx, "receipt": receipt})
            if not isinstance(receipt, dict) or str(receipt.get("transactionHash", "")).lower() != tx:
                raise CctpMismatch("RPC receipt transaction hash mismatch")
            if int(receipt.get("blockNumber", "-1"), 16) not in candidate["blocks"]:
                raise CctpMismatch("RPC receipt and Bitquery block number mismatch")
            block = rpc.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
            block_raw_id = store.save_raw("cctp_rpc_block", {"transaction": tx, "block": block})
            flow, proof = _reconcile_receipt(
                receipt, block, candidate["topics"], candidate["times"], start, end
            )
            proof.update({"receipt_raw_id": receipt_raw_id, "block_raw_id": block_raw_id})
            identity = (flow.direction, flow.message_key)
            if identity in seen_in_run and seen_in_run[identity] != tx:
                raise CctpMismatch("CCTP source-domain/nonce replay appears in multiple transactions")
            if identity in seen and seen[identity] != flow:
                raise CctpMismatch("CCTP source-domain/nonce replay conflicts with another flow")
            if identity in seen:
                previous_proof = store.get_state(f"cctp:{flow.id}")
                if previous_proof and previous_proof.get("transaction") != tx:
                    raise CctpMismatch("CCTP source-domain/nonce replay conflicts with stored provenance")
                accepted_proofs.append(proof)
                result["matched"] += 1
                continue
            seen[identity] = flow
            seen_in_run[identity] = tx
            matched.append(flow)
            accepted_proofs.append(proof)
            result["matched"] += 1
        except (CctpMismatch, KeyError, IndexError, TypeError, ValueError) as exc:
            result["unmatched"] += 1
            result["complete"] = False
            store.save_raw("cctp_unmatched", {"transaction": tx, "reason": str(exc),
                           "receipt_raw_id": receipt_raw_id, "discovery": candidate["rows"]})
    if result["complete"]:
        result["inserted"] = store.record_window("flows", start, end, matched, "bitquery+rpc")
    else:
        result["inserted"] = store.ingest(matched)
    for proof in accepted_proofs:
        provenance_raw_id = store.save_raw("cctp_provenance", proof)
        store.set_state(f"cctp:{proof['flow_id']}", {**proof,
                        "provenance_raw_id": provenance_raw_id})
    return result
