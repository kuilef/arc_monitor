"""Conformance vectors: no network, private database, or provider credentials."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "research/arc_correctness"


def validator():
    spec = importlib.util.spec_from_file_location("correctness_probe", ROOT / "validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def test_live_receipt_preserves_mirror_identity_and_sub_micro_gas():
    report = validator().build_report(ROOT / "fixtures")
    case = report["live_receipts"][0]
    assert case["status"] == "validated"
    assert case["identity"]["block_number"] == 24947854
    assert case["native_volume_base_units_18"] == "16859700000000000000"
    assert case["naive_combined_volume_base_units_18"] == "33719400000000000000"
    assert case["mirror_candidates"] == [{"native_log_index": 0, "erc20_log_indexes": [1], "status": "unique"}]
    assert int(case["gas_base_units_18"]) % 10**12 != 0
    assert case["gas_base_units_18"] != case["gas_truncated_to_6_decimals_base_units_18"]
    assert case["evidence_kind"] == "live_rpc_capture"
    assert report["live_cctp_status"] == "unknown"
    assert all(item["status"] == "unknown" for item in report["historical_receipts"])


def test_valid_source_messages_collide_only_under_the_naive_nonce_projection():
    case = validator().build_report(ROOT / "fixtures")["pending_nonce_collision"]
    assert case["status"] == "validated"
    assert case["naive_unique_keys"] == 1
    assert case["evidence_unique_keys"] == 2
    assert case["source_gross_total_base_units"] == "4000000"
    assert case["naive_keyed_gross_total_base_units"] == "2000000"
    assert len(set(case["flow_ids"])) == 2
    assert case["destination_delivery"] == "unknown"
    assert case["executed_fee"] == "unknown"
    assert case["evidence_kind"] == "synthetic_valid_protocol_vector"


def test_incoming_gross_is_net_plus_executed_fee():
    case = validator().build_report(ROOT / "fixtures")["incoming_amounts"]
    assert case["status"] == "validated"
    assert case["amounts_base_units"] == {"gross": "1000000", "recipient_net": "990000", "fee": "10000", "max_fee": "10000"}
    assert case["naive_net_as_gross_error_base_units"] == "10000"
    assert case["log_indexes"]["recipient_mint"] != case["log_indexes"]["fee_mint"]
    assert case["evidence_kind"] == "synthetic_valid_protocol_vector"


def test_equal_transfer_legs_are_ambiguous_without_an_event_identity():
    case = validator().build_report(ROOT / "fixtures")["equal_transfer_legs"]
    assert case["status"] == "ambiguous"
    assert case["reconciliation_status"] == "unknown"
    assert case["reason"] == "ambiguous_transfer_legs"
    assert case["candidate_counts"] == {"token": 2, "quote": 1}


@pytest.mark.parametrize("field", ["transactionHash", "blockHash", "blockNumber"])
def test_mismatched_log_identity_cannot_support_a_live_case(field):
    data = fixture("public-mainnet.json")
    receipt = deepcopy(data["receipts"][0]["response"]["result"])
    receipt["logs"][0][field] = "0x0"
    result = validator().validate_live_receipt(receipt, data["block"], 5042)
    assert result["status"] == "unknown"


def test_testnet_and_duplicate_log_indexes_remain_unknown():
    data = fixture("public-mainnet.json")
    receipt = deepcopy(data["receipts"][0]["response"]["result"])
    assert validator().validate_live_receipt(receipt, data["block"], 5042002)["status"] == "unknown"
    receipt["logs"][1]["logIndex"] = receipt["logs"][0]["logIndex"]
    assert validator().validate_live_receipt(receipt, data["block"], 5042)["status"] == "unknown"


def test_two_equal_native_legs_cannot_be_assigned_one_erc20_mirror():
    data = fixture("public-mainnet.json")
    receipt = deepcopy(data["receipts"][0]["response"]["result"])
    extra = deepcopy(receipt["logs"][0])
    extra["logIndex"] = "0x3"
    receipt["logs"].append(extra)
    case = validator().validate_live_receipt(receipt, data["block"], 5042)
    assert case["status"] == "ambiguous"
    assert all(item["status"] == "ambiguous" for item in case["mirror_candidates"])


def test_checked_in_report_is_the_deterministic_result():
    assert validator().build_report(ROOT / "fixtures") == json.loads((ROOT / "report.json").read_text(encoding="utf-8"))
