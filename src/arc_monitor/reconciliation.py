"""Receipt-level evidence reports for normalized trades; never creates another trade."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from .models import CHAIN_ID, Trade, USDC, address, iso


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def _topic_address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError("invalid indexed address topic")
    if value[2:26] != "0" * 24:
        raise ValueError("nonzero indexed address padding")
    return address("0x" + value[-40:])


def _erc20_transfer(log: dict):
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        return None
    data = log.get("data")
    if not isinstance(data, str) or len(data) != 66 or not data.startswith("0x"):
        return None
    try:
        return {
            "token": address(log["address"]),
            "sender": _topic_address(topics[1]),
            "receiver": _topic_address(topics[2]),
            "raw_amount": int(data, 16),
            "log_index": int(log["logIndex"], 16),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _raw_hash32(value) -> str:
    value = str(value).lower()
    if not value.startswith("0x"):
        value = "0x" + value
    if len(value) != 66:
        raise ValueError("not a bytes32 value")
    int(value[2:], 16)
    return value


def _raw_int(value) -> int:
    if isinstance(value, str) and value.lower().startswith("0x"):
        return int(value, 16)
    return int(value)


def _v4_swap_indices(logs: list[dict], manager: str, pool_id: str) -> list[int]:
    matches = []
    for log in logs:
        try:
            topics = [_raw_hash32(value) for value in log["topics"]]
            data = log["data"]
            if (address(log["address"]) != manager or len(topics) != 3
                    or topics[0] != V4_SWAP_TOPIC or topics[1] != pool_id):
                continue
            _topic_address(topics[2])
            if (not isinstance(data, str) or not data.startswith("0x")
                    or len(data) != 2 + 64 * 6):
                continue
            int(data[2:], 16)
            index = _raw_int(log["logIndex"])
            if index >= 0:
                matches.append(index)
        except (KeyError, TypeError, ValueError):
            continue
    return matches


def _v4_pool_context(store, pool_id: str, token: str, receipt_logs: list[dict]) -> dict:
    matches = []
    with store.engine.connect() as conn:
        rows = conn.execute(select(store.raw.c.id, store.raw.c.source, store.raw.c.payload)).all()
    for raw_id, source, payload in rows:
        if source != "bitquery" and not str(source).startswith("bitquery:"):
            continue
        try:
            body = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        candidates = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            header, topics = candidate.get("LogHeader"), candidate.get("Topics")
            block, transaction = candidate.get("Block"), candidate.get("Transaction")
            if (not isinstance(header, dict) or not isinstance(topics, list)
                    or not isinstance(block, dict) or not isinstance(transaction, dict)):
                continue
            try:
                hashes = [_raw_hash32(item["Hash"]) for item in topics]
                if len(hashes) != 4:
                    continue
                currencies = {_topic_address(hashes[2]), _topic_address(hashes[3])}
                data = header["Data"]
                if (hashes[0] != V4_INITIALIZE_TOPIC
                        or hashes[1] != pool_id or currencies != {token, USDC}
                        or not isinstance(data, str) or not data.startswith("0x")
                        or len(data) != 2 + 64 * 5 or header.get("Removed") is not False):
                    continue
                int(data[2:], 16)
                block_number = _raw_int(block["Number"])
                provider_log_index = _raw_int(header["Index"])
                if block_number < 0 or provider_log_index < 0:
                    continue
                manager = address(header["Address"])
                swap_indices = _v4_swap_indices(receipt_logs, manager, pool_id)
                if len(swap_indices) != 1:
                    continue
                match = {
                    "kind": "uniswap_v4", "pool_id": pool_id,
                    "manager": manager, "raw_id": raw_id,
                    "event": "Initialize", "event_topic": V4_INITIALIZE_TOPIC,
                    "transaction": _raw_hash32(transaction["Hash"]),
                    "block_number": block_number,
                    "provider_log_index": provider_log_index,
                    "swap_event_topic": V4_SWAP_TOPIC,
                    "swap_log_index": swap_indices[0],
                }
            except (KeyError, TypeError, ValueError):
                continue
            matches.append(match)
    unique = {json.dumps(item, sort_keys=True): item for item in matches}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return {"kind": "uniswap_v4", "pool_id": pool_id, "manager": None,
            "status": "missing" if not unique else "ambiguous"}


def _receipt_log_problem(receipt: dict, tx: str, block_hash: str, block_number: int):
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return "malformed_receipt_logs"
    for log in logs:
        if not isinstance(log, dict):
            return "malformed_receipt_logs"
        if log.get("removed") is not False:
            return "removed_receipt_log" if log.get("removed") is True else "malformed_receipt_logs"
        try:
            same_identity = (
                _raw_hash32(log["transactionHash"]) == tx.lower()
                and _raw_hash32(log["blockHash"]) == block_hash
                and _raw_int(log["blockNumber"]) == block_number
            )
        except (KeyError, TypeError, ValueError):
            return "malformed_receipt_logs"
        if not same_identity:
            return "receipt_log_identity_mismatch"
    return None


def _decimals(rpc, token: str, block: str) -> int:
    raw = rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, block])
    if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
        raise ValueError("decimals call did not return uint256")
    value = int(raw, 16)
    if not 0 <= value <= 36:
        raise ValueError("unsupported decimals")
    return value


def _expected_raw(amount: Decimal, decimals: int):
    scaled = amount * (Decimal(10) ** decimals)
    integer = int(scaled)
    return int(integer), scaled - integer


def reconcile_trade(store, rpc, trade) -> dict:
    """Compare one normalized trade to one verified receipt without mutating the trade."""
    if not isinstance(trade, Trade):
        trade = Trade.model_validate(trade)
    chain = rpc.verify()
    if chain.get("chain_id") != CHAIN_ID or chain.get("verified") is not True:
        raise ValueError("RPC chain was not verified as Arc")

    report = {
        "trade_id": trade.id,
        "original_trade": trade.model_dump(mode="json"),
        "chain_verified": True,
        "status": "unknown",
    }

    def finish(reason=None):
        if reason:
            report["reason"] = reason
        store.set_state(f"reconciliation:{trade.id}", report)
        return report

    receipt = rpc.call("eth_getTransactionReceipt", [trade.tx])
    report["receipt_raw_id"] = store.save_raw("rpc:transaction_receipt", receipt)
    if not isinstance(receipt, dict):
        return finish("malformed_receipt")
    if str(receipt.get("transactionHash", "")).lower() != trade.tx.lower():
        return finish("receipt_transaction_mismatch")
    try:
        status = int(receipt["status"], 16) if isinstance(receipt["status"], str) else int(receipt["status"])
    except (KeyError, TypeError, ValueError):
        return finish("receipt_status_missing")
    if status != 1:
        return finish("transaction_failed")
    try:
        block_number = int(receipt["blockNumber"], 16)
        block_hash = str(receipt["blockHash"]).lower()
        block_tag = hex(block_number)
        block = rpc.call("eth_getBlockByNumber", [block_tag, False])
        if (int(block["number"], 16) != block_number
                or str(block["hash"]).lower() != block_hash):
            return finish("receipt_block_mismatch")
        block_time = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
    except (KeyError, TypeError, ValueError):
        return finish("malformed_receipt_block")
    report["receipt"] = {"block_number": block_number, "block_hash": block_hash,
                         "block_time": iso(block_time),
                         "trade_time_difference_seconds": str((block_time - trade.ts).total_seconds())}
    if block_time != trade.ts:
        return finish("block_time_mismatch")

    log_problem = _receipt_log_problem(receipt, trade.tx, block_hash, block_number)
    if log_problem:
        return finish(log_problem)

    if len(trade.pool) == 66 and trade.pool.startswith("0x"):
        pool_context = _v4_pool_context(store, trade.pool.lower(), trade.token, receipt["logs"])
        pool_address = pool_context.get("manager")
    else:
        try:
            pool_address = address(trade.pool)
        except ValueError:
            pool_address = None
        pool_context = {"kind": "address_pool", "address": pool_address}
    report["pool_context"] = pool_context
    if not pool_address:
        return finish("pool_identity_unresolved")

    try:
        token_decimals = _decimals(rpc, trade.token, block_tag)
        quote_decimals = _decimals(rpc, USDC, block_tag)
    except (TypeError, ValueError, RuntimeError):
        return finish("decimals_unavailable_at_receipt_block")
    report["decimals"] = {"token": token_decimals, "quote": quote_decimals,
                           "block_number": block_number}

    parsed = [item for item in (_erc20_transfer(log) for log in receipt.get("logs", [])) if item]
    if trade.side == "buy":
        token_pair, quote_pair = (pool_address, trade.wallet), (trade.wallet, pool_address)
    else:
        token_pair, quote_pair = (trade.wallet, pool_address), (pool_address, trade.wallet)
    token_legs = [item for item in parsed if item["token"] == trade.token
                  and (item["sender"], item["receiver"]) == token_pair]
    quote_legs = [item for item in parsed if item["token"] == USDC
                  and (item["sender"], item["receiver"]) == quote_pair]
    report["candidate_counts"] = {"token": len(token_legs), "quote": len(quote_legs)}
    if len(token_legs) != 1 or len(quote_legs) != 1:
        return finish("ambiguous_transfer_legs")

    token_expected, token_rounding = _expected_raw(trade.quantity, token_decimals)
    quote_expected, quote_rounding = _expected_raw(trade.quote_amount, quote_decimals)
    report["provider_conversion_residual_raw_units"] = {
        "token": format(token_rounding, "f"), "quote": format(quote_rounding, "f")
    }
    report["token_leg"] = {**token_legs[0], "raw_amount": str(token_legs[0]["raw_amount"]),
                           "expected_raw_amount": str(token_expected)}
    report["quote_leg"] = {**quote_legs[0], "raw_amount": str(quote_legs[0]["raw_amount"]),
                            "expected_raw_amount": str(quote_expected)}
    report["raw_amount_difference"] = {
        "token": str(token_legs[0]["raw_amount"] - token_expected),
        "quote": str(quote_legs[0]["raw_amount"] - quote_expected),
    }
    if token_rounding or quote_rounding:
        return finish("normalized_amount_not_exact_integer_units")
    if token_legs[0]["raw_amount"] != token_expected or quote_legs[0]["raw_amount"] != quote_expected:
        report["status"] = "mismatch"
        return finish("amount_mismatch")
    report["status"] = "matched"
    return finish()
