"""Bounded enrichment of tokens and buyers already observed by the collector."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .budget import BudgetExceeded
from .models import (BURN, CHAIN_ID, ZERO, Funding, Holder, Holders, Risk, USDC,
                     address, iso, parse_time, units)
from .providers.bitquery import ProviderError


_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)


def _holders_query(token: str, limit: int) -> str:
    return f"""query {{ EVM(network: arc) {{ Holders(
      limit: {{count:{limit}}}, orderBy: {{descending: Balance_Amount}},
      where: {{Currency: {{SmartContract: {{is:\"{token}\"}}}}, Balance: {{Amount: {{gt:"0"}}}}}}
    ) {{ Holder {{Address}} Balance {{Amount FirstChangeTime LastChangeTime}} }} }} }}"""


def _funding_query(wallet: str, start: datetime, end: datetime) -> str:
    # ``till`` is inclusive at Bitquery, while validation below enforces [start, end).
    return f"""query {{ EVM(network: arc) {{ Transfers(
      limit: {{count:100}}, orderBy: {{ascending: Block_Time}},
      where: {{Block: {{Time: {{since:\"{iso(start)}\", till:\"{iso(end)}\"}}}},
        TransactionStatus: {{Success:true}}, Transfer: {{Success:true,
          Receiver: {{is:\"{wallet}\"}}, Amount: {{gt:"0"}},
          Currency: {{SmartContract: {{is:\"{USDC}\"}}, Native:false}}}}}}
    ) {{ Block {{Number Time}} Transaction {{Hash}} TransactionStatus {{Success}}
         Transfer {{Amount Sender Receiver Success Currency {{SmartContract Native Decimals}}}} }} }} }}"""


def _uint256(raw) -> int:
    if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
        raise ValueError("expected a 32-byte RPC uint")
    return int(raw, 16)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _rpc_contract_evidence(rpc, token: str) -> dict:
    block_tag = rpc.call("eth_blockNumber")
    block_number = int(block_tag, 16)
    block = rpc.call("eth_getBlockByNumber", [block_tag, False])
    if not isinstance(block, dict) or int(block.get("number", "-1"), 16) != block_number:
        raise ValueError("RPC block response does not match the pinned block")
    block_hash = str(block.get("hash", "")).lower()
    if len(block_hash) != 66:
        raise ValueError("RPC block is missing a valid block hash")
    code = rpc.call("eth_getCode", [token, block_tag])
    if not isinstance(code, str) or not code.startswith("0x"):
        raise ValueError("RPC code response is malformed")
    result = {
        "block_number": block_number,
        "block_tag": block_tag,
        "block_hash": block_hash,
        "block_time": iso(datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)),
        "has_code": code not in ("0x", "0x0"),
        "code_sha256": hashlib.sha256(bytes.fromhex(code[2:])).hexdigest() if len(code) > 2 else None,
        "erc20": {"decimals": None, "total_supply": None},
        "owner": None,
        "proxy": {"status": "unknown", "implementation": None},
        "limits": "Interface and storage checks are evidence, not a security audit.",
    }
    if not result["has_code"]:
        return result

    try:
        decimals = _uint256(rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, block_tag]))
        if not 0 <= decimals <= 36:
            raise ValueError("ERC20 decimals are outside supported range")
        supply_raw = _uint256(rpc.call("eth_call", [{"to": token, "data": "0x18160ddd"}, block_tag]))
        result["erc20"] = {
            "decimals": decimals,
            "total_supply": _decimal_text(units(supply_raw, decimals)),
        }
        result["total_supply"] = result["erc20"]["total_supply"]
    except BudgetExceeded:
        raise
    except (TypeError, ValueError, RuntimeError) as exc:
        result["erc20"]["error"] = type(exc).__name__

    try:
        owner_raw = rpc.call("eth_call", [{"to": token, "data": "0x8da5cb5b"}, block_tag])
        owner_int = _uint256(owner_raw)
        if owner_int:
            result["owner"] = address("0x" + owner_raw[-40:])
    except BudgetExceeded:
        raise
    except (TypeError, ValueError, RuntimeError) as exc:
        result["owner_check"] = "unavailable:" + type(exc).__name__

    try:
        implementation_raw = rpc.call("eth_getStorageAt", [token, _IMPLEMENTATION_SLOT, block_tag])
        implementation_int = _uint256(implementation_raw)
        if implementation_int:
            result["proxy"] = {
                "status": "eip1967_implementation_slot_set",
                "implementation": address("0x" + implementation_raw[-40:]),
            }
        else:
            result["proxy"] = {"status": "no_known_slot_value", "implementation": None}
    except BudgetExceeded:
        raise
    except (TypeError, ValueError, RuntimeError) as exc:
        result["proxy"] = {
            "status": "storage_check_unavailable",
            "implementation": None,
            "error": type(exc).__name__,
        }
    return result


def _holder_role(wallet: str, pools: set[str]) -> str:
    if wallet in (ZERO, BURN):
        return "burn"
    if wallet in pools:
        return "pool"
    return "wallet"


def _normalize_holders(rows: list[dict], *, token: str, pools: set[str], limit: int,
                       now: datetime, raw_id: str) -> tuple[Holders, dict]:
    accounts = []
    rejected = 0
    seen = set()
    for row in rows[:limit]:
        try:
            wallet = address(row["Holder"]["Address"])
            balance = Decimal(str(row["Balance"]["Amount"]))
            if not balance.is_finite() or balance <= 0 or wallet in seen:
                raise ValueError
            seen.add(wallet)
            accounts.append(Holder(wallet=wallet, balance=balance, role=_holder_role(wallet, pools)))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            rejected += 1
    saturated = len(rows) > limit
    evidence = {
        "raw_id": raw_id,
        "scope": "current_positive_token_balances",
        "requested_limit": limit,
        "returned_rows": len(rows),
        "accepted_rows": len(accounts),
        "rejected_rows": rejected,
        "saturated": saturated,
        "complete": False,
        "completeness_reason": (
            "response_truncated_at_limit_plus_one" if saturated
            else "holder response is not pinned to the verified supply block"
        ),
        "observed_at": iso(now),
    }
    snapshot = Holders(
        id=f"bitquery-holders:{token}:{iso(now)}",
        token=token,
        ts=now,
        source="bitquery-current",
        accounts=accounts,
        complete=False,
        total_supply=None,
    )
    return snapshot, evidence


def _normalize_funding(row: dict, *, wallet: str, start: datetime, purchase: datetime):
    transfer = row["Transfer"]
    currency = transfer["Currency"]
    at = parse_time(row["Block"]["Time"])
    if not start <= at < purchase:
        raise ValueError("outside requested historical window")
    if row.get("TransactionStatus", {}).get("Success") is not True or transfer.get("Success") is not True:
        raise ValueError("unsuccessful transfer")
    if address(transfer["Receiver"]) != wallet:
        raise ValueError("wrong receiver")
    if currency.get("Native") is not False or address(currency["SmartContract"]) != USDC:
        raise ValueError("wrong currency representation")
    if int(currency.get("Decimals")) != 6:
        raise ValueError("unexpected USDC decimals")
    amount = Decimal(str(transfer["Amount"]))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("invalid funding amount")
    sender = address(transfer["Sender"])
    tx = str(row["Transaction"]["Hash"]).lower()
    if len(tx) != 66 or not tx.startswith("0x"):
        raise ValueError("invalid transaction hash")
    ident = hashlib.sha256(f"{tx}:{wallet}:{sender}:{amount}:{iso(at)}".encode()).hexdigest()
    return Funding(id="bitquery-funding:" + ident, ts=at, source="bitquery",
                   wallet=wallet, funder=sender, amount_usdc=amount)


def _source_failure(source: str, error: Exception) -> dict:
    if isinstance(error, ProviderError):
        kind = "provider_error"
    elif isinstance(error, (ValueError, TypeError, KeyError, InvalidOperation)):
        kind = "invalid_response"
    else:
        kind = "runtime_error"
    return {"status": "failed", "source": source, "error_type": kind,
            "retryable": bool(getattr(error, "retryable", False))}


def _completion_time(clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("enrichment clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _rotate(store, category: str, universe: list[str], limit: int) -> list[str]:
    """Persist an attempt sequence so repeated bounded runs cannot starve later items."""
    rotation = store.get_state("enrichment:rotation") or {
        "version": 1, "sequence": 0, "tokens": {}, "wallets": {}
    }
    rotation.setdefault("tokens", {})
    rotation.setdefault("wallets", {})
    marks = rotation[category]
    position = {item: index for index, item in enumerate(universe)}
    selected = sorted(universe, key=lambda item: (marks.get(item, -1), position[item]))[:limit]
    for item in selected:
        rotation["sequence"] = int(rotation.get("sequence", 0)) + 1
        marks[item] = rotation["sequence"]
    store.set_state("enrichment:rotation", rotation)
    return selected


def enrich_observed(store, provider, rpc, *, max_tokens=3, max_wallets=10,
                    holder_limit=1000, funding_hours=24, gecko=None, now=None, _clock=None) -> dict:
    """Enrich only already-observed tokens and buyer wallets within fixed request bounds."""
    if not 1 <= max_tokens <= 100 or not 0 <= max_wallets <= 1000:
        raise ValueError("invalid token or wallet bound")
    if not 1 <= holder_limit <= 10_000 or not 0 < funding_hours <= 24 * 365:
        raise ValueError("invalid holder limit or funding window")
    clock = _clock or (lambda: datetime.now(timezone.utc))
    if now is not None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        selection_time = now.astimezone(timezone.utc)
    else:
        selection_time = _completion_time(clock)

    launches = [item for item in store.events("launch") if item.ts <= selection_time]
    trades = [item for item in store.events("trade") if item.ts <= selection_time]
    universe = []
    for item in [*launches, *trades]:
        if item.token not in universe:
            universe.append(item.token)

    if universe:
        try:
            chain = rpc.verify()
            if chain.get("chain_id") != CHAIN_ID or chain.get("verified") is not True:
                raise ValueError("RPC chain was not verified as Arc")
            rpc_status = {"status": "ok", "source": "rpc_chain", "chain_id": CHAIN_ID}
        except BudgetExceeded:
            raise
        except (ProviderError, ValueError, TypeError, KeyError, RuntimeError) as error:
            rpc_status = _source_failure("rpc_chain", error)
    else:
        rpc_status = {"status": "skipped", "source": "rpc_chain", "reason": "no_eligible_tokens"}
    tokens = _rotate(store, "tokens", universe, max_tokens)

    result = {"observed_at": iso(selection_time), "rpc": rpc_status,
              "tokens": [], "funding": {}, "creator_history": {}}
    for token in tokens:
        token_launches = [item for item in launches if item.token == token]
        pools = {item.pool.lower() for item in token_launches
                 if item.pool and len(item.pool) == 42 and item.pool.startswith("0x")}
        token_report = {
            "token": token,
            "attempted_at": iso(_completion_time(clock)),
            "holders": {"status": "pending"},
            "contract": {"status": "pending"},
            "liquidity": {"status": "pending"},
        }

        try:
            if rpc_status["status"] != "ok":
                contract = dict(rpc_status)
                contract_at = _completion_time(clock)
                risk_evidence = ("Contract RPC evidence was unavailable. Taxes, dangerous permissions, "
                                 "and LP lock remain unknown; no security audit claim is made.")
            else:
                contract = _rpc_contract_evidence(rpc, token)
                contract_at = _completion_time(clock)
                raw_id = store.save_raw("rpc:contract_evidence", {
                    "token": token, "observed_at": iso(contract_at), "evidence": contract,
                })
                partial = (contract.get("erc20", {}).get("error") or contract.get("owner_check")
                           or contract.get("proxy", {}).get("status") == "storage_check_unavailable")
                contract = {"status": "partial" if partial else "ok", "source": "rpc",
                            "raw_id": raw_id, **contract}
                risk_evidence = ("Taxes, dangerous permissions, and LP lock remain unknown. "
                                 "Pinned interface, owner, code, and proxy-slot checks are not a security audit.")
        except BudgetExceeded:
            raise
        except (ProviderError, ValueError, TypeError, KeyError, RuntimeError) as error:
            contract_at = _completion_time(clock)
            contract = _source_failure("rpc_contract", error)
            risk_evidence = ("Contract RPC evidence was unavailable. Taxes, dangerous permissions, "
                             "and LP lock remain unknown; no security audit claim is made.")
        store.ingest([Risk(
            id=f"contract-risk:{token}:{iso(contract_at)}", token=token, ts=contract_at,
            source="rpc-interface-evidence", evidence=risk_evidence,
        )])
        token_report["contract"] = contract
        store.set_state(f"enrichment:{token}", token_report)

        query = _holders_query(token, holder_limit + 1)
        try:
            rows = provider.execute(query, "EVM", "Holders")
            holders_at = _completion_time(clock)
            raw_id = store.save_raw("bitquery:holders", {
                "query": query, "rows": rows, "observed_at": iso(holders_at), "token": token,
            })
            holders, holder_evidence = _normalize_holders(
                rows, token=token, pools=pools, limit=holder_limit, now=holders_at, raw_id=raw_id,
            )
            store.ingest([holders])
            token_report["holders"] = {"status": "ok", "source": "bitquery_holders",
                                        **holder_evidence}
        except BudgetExceeded:
            raise
        except (ProviderError, ValueError, TypeError, KeyError, RuntimeError) as error:
            token_report["holders"] = _source_failure("bitquery_holders", error)
        store.set_state(f"enrichment:{token}", token_report)

        known_pool = next((item.pool for item in reversed(token_launches) if item.pool), None)
        if gecko is None or not known_pool:
            token_report["liquidity"] = {
                "status": "skipped", "source": "geckoterminal",
                "reason": "no_injected_source" if gecko is None else "no_known_pool",
            }
        else:
            try:
                liquidity = gecko.pool_liquidity(token, known_pool, observed_at=None)
                liquidity_at = _completion_time(clock)
                liquidity = liquidity.model_copy(update={
                    "id": f"enrichment-liquidity:{token}:{known_pool}:{iso(liquidity_at)}",
                    "ts": liquidity_at,
                })
                store.ingest([liquidity])
                token_report["liquidity"] = {
                    "status": "ok", "source": "geckoterminal", **liquidity.model_dump(mode="json")
                }
            except BudgetExceeded:
                raise
            except (ProviderError, ValueError, TypeError, KeyError, RuntimeError) as error:
                token_report["liquidity"] = _source_failure("geckoterminal", error)
        store.set_state(f"enrichment:{token}", token_report)
        result["tokens"].append(token_report)

    buyers = {}
    for item in trades:
        if item.side == "buy" and item.ts <= selection_time:
            buyers[item.wallet] = min(item.ts, buyers.get(item.wallet, item.ts))
    wallet_universe = [wallet for wallet, _ in sorted(buyers.items(), key=lambda pair: (pair[1], pair[0]))]
    selected_wallets = _rotate(store, "wallets", wallet_universe, max_wallets)
    selected = [(wallet, buyers[wallet]) for wallet in selected_wallets]
    rejected = 0
    funding_raw = []
    observed = 0
    result["funding"] = {
        "scope": "earliest_observed_incoming_erc20_usdc_within_pre_purchase_window",
        "window_hours": funding_hours,
        "wallets_available": len(buyers),
        "wallets_considered": len(selected),
        "wallet_limit_saturated": len(buyers) > max_wallets,
        "observed": 0,
        "rejected_rows": 0,
        "queries": funding_raw,
    }
    store.set_state("enrichment:funding", result["funding"])
    for wallet, purchase in selected:
        start = purchase - timedelta(hours=funding_hours)
        query = _funding_query(wallet, start, purchase)
        try:
            rows = provider.execute(query, "EVM", "Transfers")
            raw_id = store.save_raw("bitquery:funding", {
                "query": query, "rows": rows, "wallet": wallet,
                "window": {"start": iso(start), "end_exclusive": iso(purchase)},
            })
            valid = []
            query_rejected = 0
            for row in rows:
                try:
                    valid.append(_normalize_funding(row, wallet=wallet, start=start, purchase=purchase))
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    query_rejected += 1
            rejected += query_rejected
            if valid:
                store.ingest([min(valid, key=lambda item: (item.ts, item.id))])
                observed += 1
            funding_raw.append({"status": "ok", "source": "bitquery_funding",
                                "wallet": wallet, "raw_id": raw_id, "returned_rows": len(rows),
                                "valid_rows": len(valid), "rejected_rows": query_rejected,
                                "saturated": len(rows) >= 100,
                                "window": {"start": iso(start), "end_exclusive": iso(purchase)}})
        except BudgetExceeded:
            raise
        except (ProviderError, ValueError, TypeError, KeyError, RuntimeError) as error:
            funding_raw.append({"wallet": wallet, **_source_failure("bitquery_funding", error),
                                "window": {"start": iso(start), "end_exclusive": iso(purchase)}})
        result["funding"].update(observed=observed, rejected_rows=rejected)
        store.set_state("enrichment:funding", result["funding"])

    creators = sorted({item.creator for item in launches if item.creator})
    for creator in creators:
        scoped = [item for item in launches if item.creator == creator]
        result["creator_history"][creator] = {
            "scope": "launches_observed_in_this_store",
            "observed_launches": len(scoped),
            "token_count": len({item.token for item in scoped}),
            "first_observed": iso(min(item.ts for item in scoped)),
            "last_observed": iso(max(item.ts for item in scoped)),
            "complete_history": False,
        }
    store.set_state("enrichment:creator_history", result["creator_history"])
    result["observed_at"] = iso(_completion_time(clock))
    return result
