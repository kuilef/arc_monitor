"""Offline research probe. Reuses existing mainnet decoders; no RPC or .env access."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from arc_monitor.cctp import MESSAGE_RECEIVED_TOPIC, MESSAGE_SENT_TOPIC, _message_key, _reconcile_receipt
from arc_monitor.models import CHAIN_ID, USDC
from arc_monitor.reconciliation import _erc20_transfer, _raw_hash32, _receipt_log_problem, reconcile_trade
from arc_monitor.storage import Store

NATIVE = "0xfffffffffffffffffffffffffffffffffffffffe"
SCALE = 10**12
SYNTHETIC = "synthetic_valid_protocol_vector"


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_live_receipt(receipt, block, chain_id):
    """Keep canonical movement amounts while refusing uncertain mirror assignments."""
    result = {"evidence_kind": "live_rpc_capture", "status": "unknown"}
    if chain_id != CHAIN_ID:
        return {**result, "reason": "unsupported_chain"}
    try:
        tx = _raw_hash32(receipt["transactionHash"])
        block_hash = _raw_hash32(receipt["blockHash"])
        number = int(receipt["blockNumber"], 16)
        if receipt["status"] != "0x1":
            return {**result, "reason": "transaction_unsuccessful"}
        if (block_hash != _raw_hash32(block["hash"]) or number != int(block["number"], 16)
                or tx not in [item.lower() for item in block["transactions"]]):
            return {**result, "reason": "receipt_block_identity_mismatch"}
        problem = _receipt_log_problem(receipt, tx, block_hash, number)
        if problem:
            return {**result, "reason": problem}
        indexes = [int(item["logIndex"], 16) for item in receipt["logs"]]
        if len(set(indexes)) != len(indexes) or any(index < 0 for index in indexes):
            return {**result, "reason": "duplicate_or_invalid_log_index"}
        selected = [item for item in receipt["logs"] if item["address"].lower() in {NATIVE, USDC}]
        transfers = [_erc20_transfer(item) for item in selected]
        if any(item is None for item in transfers):
            return {**result, "reason": "unsupported_usdc_log"}
        gas = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    except (KeyError, TypeError, ValueError):
        return {**result, "reason": "malformed_evidence"}

    native = [item for item in transfers if item["token"] == NATIVE]
    erc20 = [item for item in transfers if item["token"] == USDC]

    def signature(item, scale=1):
        return item["sender"], item["receiver"], item["raw_amount"] * scale

    pairs = []
    for item in native:
        candidates = [other["log_index"] for other in erc20 if signature(other, SCALE) == signature(item)]
        native_count = sum(signature(other) == signature(item) for other in native)
        status = "unique" if len(candidates) == 1 and native_count == 1 else "ambiguous" if candidates else "none"
        pairs.append({"native_log_index": item["log_index"], "erc20_log_indexes": candidates, "status": status})
    orphan_erc20 = [item["log_index"] for item in erc20
                   if not any(signature(item, SCALE) == signature(other) for other in native)
                   and item["raw_amount"] and item["sender"] != item["receiver"]]
    return {
        **result,
        "status": "unknown" if orphan_erc20 else "ambiguous" if any(p["status"] == "ambiguous" for p in pairs)
        else "validated",
        "identity": {"chain_id": chain_id, "transaction_hash": tx, "block_hash": block_hash,
                     "block_number": number, "log_indexes": indexes},
        "transfers": [{**item, "raw_amount": str(item["raw_amount"]),
                       "decimals": 18 if item["token"] == NATIVE else 6} for item in transfers],
        "canonical_movement_count": len(native),
        "native_volume_base_units_18": str(sum(item["raw_amount"] for item in native)),
        "naive_combined_volume_base_units_18": str(sum(item["raw_amount"] for item in native)
                                                   + sum(item["raw_amount"] * SCALE for item in erc20)),
        "mirror_candidates": pairs,
        "unpaired_erc20_log_indexes": orphan_erc20,
        "gas_base_units_18": str(gas),
        "gas_truncated_to_6_decimals_base_units_18": str(gas // SCALE * SCALE),
        "gas_precision_lost_base_units_18": str(gas % SCALE),
        "gas_scope": "receipt_fee_paid_by_transaction_sender_separate_from_transfer_volume",
    }


def cctp_proof(receipt, block, topic):
    ts = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc)
    flow, proof = _reconcile_receipt(receipt, block, {topic}, {ts}, ts, ts + timedelta(seconds=1))
    return {"flow": flow.model_dump(mode="json"), "proof": proof}


class FixtureRpc:
    """Only the deterministic responses needed by the existing trade reconciler."""
    def __init__(self, data):
        self.data = data

    def verify(self):
        return {"chain_id": CHAIN_ID, "verified": True}

    def call(self, method, params):
        if method == "eth_getTransactionReceipt":
            assert params == [self.data["ambiguous_trade"]["tx"]]
            return self.data["ambiguous_receipt"]
        if method == "eth_getBlockByNumber":
            assert params == [self.data["ambiguous_block"]["number"], False]
            return self.data["ambiguous_block"]
        if method == "eth_call":
            assert params[0]["data"] == "0x313ce567" and params[1] == self.data["ambiguous_block"]["number"]
            return "0x" + format(6 if params[0]["to"].lower() == USDC else 18, "064x")
        raise AssertionError(method)


def build_report(fixtures):
    fixtures = Path(fixtures)
    public = json.loads((fixtures / "public-mainnet.json").read_text(encoding="utf-8"))
    synthetic = json.loads((fixtures / "synthetic-protocol.json").read_text(encoding="utf-8"))
    assert public["evidence_kind"] == "live_rpc_capture" and synthetic["evidence_kind"] == SYNTHETIC
    chain = int(public["provenance"]["chain_id_response"]["result"], 16)
    outgoing = [cctp_proof(receipt, synthetic["block"], MESSAGE_SENT_TOPIC) for receipt in synthetic["outgoing"]]
    incoming = cctp_proof(synthetic["incoming"], synthetic["block"], MESSAGE_RECEIVED_TOPIC)
    with Store(":memory:", mode="demo") as store:
        ambiguous = reconcile_trade(store, FixtureRpc(synthetic), synthetic["ambiguous_trade"])
    proof = incoming["proof"]
    return {
        "schema_version": 1,
        "scope": "finite offline Arc mainnet receipt/CCTP evidence conformance probe",
        "decision": {
            "nontrivial_reproducible_counterexample": "valid_source_messages_collide_under_source_domain_zero_nonce",
            "counterexample_evidence_kind": SYNTHETIC,
            "external_service_bug_proven": False,
            "standalone_project": "not_justified_yet",
            "next_step": "keep_small_corpus_in_existing_repo; require_public_live_CCTP_evidence_before_expansion",
        },
        "fixture_sha256_canonical_json": {
            "public-mainnet.json": canonical_sha256(public),
            "synthetic-protocol.json": canonical_sha256(synthetic),
        },
        "rpc_provenance": public["provenance"],
        "live_receipts": [validate_live_receipt(item["response"]["result"], public["block"], chain)
                          for item in public["receipts"]],
        "historical_receipts": [{"transaction_hash": item["transaction"], "status": "unknown",
                                 "reason": "anonymous_rpc_returned_null"}
                                for item in public["provenance"]["historical_receipts"]],
        "live_cctp_status": "unknown",
        "live_cctp_reason": "no_events_in_single_bounded_256_block_discovery_request",
        "pending_nonce_collision": {
            "evidence_kind": SYNTHETIC, "status": "validated",
            "naive_unique_keys": len({_message_key(26, bytes(32)) for _ in outgoing}),
            "evidence_unique_keys": len({item["flow"]["message_key"] for item in outgoing}),
            "source_gross_total_base_units": str(sum(int(item["proof"]["amounts_base_units"]["gross"])
                                                      for item in outgoing)),
            "naive_keyed_gross_total_base_units": str(sum({_message_key(26, bytes(32)):
                int(item["proof"]["amounts_base_units"]["gross"]) for item in outgoing}.values())),
            "flow_ids": [item["flow"]["id"] for item in outgoing],
            "source_evidence": outgoing, "destination_delivery": "unknown", "executed_fee": "unknown",
        },
        "incoming_amounts": {
            "evidence_kind": SYNTHETIC, "status": "validated", "identity": proof,
            "amounts_base_units": proof["amounts_base_units"], "log_indexes": proof["log_indexes"],
            "naive_net_as_gross_error_base_units": str(int(proof["amounts_base_units"]["gross"])
                                                       - int(proof["amounts_base_units"]["recipient_net"])),
        },
        "equal_transfer_legs": {
            "evidence_kind": SYNTHETIC, "status": "ambiguous", "reconciliation_status": ambiguous["status"],
            "reason": ambiguous["reason"], "candidate_counts": ambiguous["candidate_counts"],
            "identity": {"transaction_hash": synthetic["ambiguous_receipt"]["transactionHash"],
                         "block_hash": synthetic["ambiguous_receipt"]["blockHash"],
                         "block_number": int(synthetic["ambiguous_receipt"]["blockNumber"], 16),
                         "log_indexes": [int(item["logIndex"], 16)
                                         for item in synthetic["ambiguous_receipt"]["logs"]]},
        },
        "limitations": ["single_anonymous_RPC_not_independent_consensus_proof", "no_raw_Bitquery_responses",
                        "synthetic_CCTP_and_trade_vectors_not_live_service_failures", "mainnet_only_5042",
                        "uses_existing_private_decoder_functions_pinned_to_base_commit",
                        "no_attestation_or_destination_chain_delivery_proof", "no_new_license_grant"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare with the committed report without writing")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    report = build_report(root / "fixtures")
    target = root / "report.json"
    if args.check:
        if report != json.loads(target.read_text(encoding="utf-8")):
            raise SystemExit("Report differs from deterministic fixture replay")
        print("Report matches deterministic fixture replay")
    else:
        target.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(target)


if __name__ == "__main__":
    main()
