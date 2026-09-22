from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arc_monitor.models import CHAIN_ID, USDC, Launch, Liquidity, Trade
from arc_monitor.storage import Store
from test_storage import A, T, W


POOL = "0x" + "9" * 40
FUNDER = "0x" + "8" * 40


def observed_store():
    store = Store(":memory:", mode="demo")
    store.ingest([
        Launch(id="launch", token=A, ts=T, source="test", tx="0x" + "a" * 64,
               creator=W, pool=POOL),
        Trade(id="buy", token=A, ts=T + timedelta(hours=2), source="test",
              tx="0x" + "b" * 64, wallet=W, side="buy", quantity="5",
              quote_amount="10", pool=POOL),
    ])
    return store


class Provider:
    def __init__(self, holders, transfers):
        self.holders = holders
        self.transfers = transfers
        self.calls = []

    def execute(self, query, root, cube):
        self.calls.append((query, root, cube))
        return self.holders if cube == "Holders" else self.transfers


class Rpc:
    def __init__(self):
        self.calls = []

    def verify(self):
        return {"chain_id": CHAIN_ID, "verified": True}

    def call(self, method, params=None):
        self.calls.append((method, params or []))
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getBlockByNumber":
            return {"number": "0x64", "hash": "0x" + "c" * 64,
                    "timestamp": hex(int((T + timedelta(hours=3)).timestamp()))}
        if method == "eth_getCode":
            return "0x60016000"
        if method == "eth_getStorageAt":
            return "0x" + "0" * 64
        selector = params[0]["data"]
        if selector == "0x313ce567":
            return "0x" + format(18, "064x")
        if selector == "0x18160ddd":
            return "0x" + format(10**24, "064x")
        if selector == "0x8da5cb5b":
            return "0x" + "0" * 24 + W[2:]
        raise AssertionError((method, params))


class Gecko:
    def pool_liquidity(self, token, pool, *, observed_at):
        observed_at = observed_at or datetime.now(timezone.utc)
        return Liquidity(id=f"gecko:{pool}:{observed_at.isoformat()}", token=token,
                         ts=observed_at, source="geckoterminal", pool=pool,
                         amount_usdc="1234.5", valuation="usd_assuming_usdc_peg")


def holder(wallet, amount):
    return {"Holder": {"Address": wallet},
            "Balance": {"Amount": amount,
                        "FirstChangeTime": T.isoformat(),
                        "LastChangeTime": (T + timedelta(hours=1)).isoformat()}}


def transfer(*, at, sender=FUNDER, receiver=W, token=USDC, native=False,
             amount="7.5", success=True):
    return {
        "Block": {"Number": 50, "Time": at.isoformat().replace("+00:00", "Z")},
        "Transaction": {"Hash": "0x" + format(int(at.timestamp()), "064x")[-64:]},
        "TransactionStatus": {"Success": success},
        "Transfer": {"Amount": amount, "Sender": sender, "Receiver": receiver,
                     "Success": success,
                     "Currency": {"SmartContract": token, "Native": native, "Decimals": 6}},
    }


def test_enrichment_marks_saturated_holders_partial_and_saves_provenance():
    from arc_monitor.enrichment import enrich_observed

    provider = Provider([holder(W, "5"), holder(FUNDER, "4"), holder("0x" + "7" * 40, "3")], [])
    with observed_store() as store:
        result = enrich_observed(store, provider, Rpc(), holder_limit=2,
                                 funding_hours=1, gecko=Gecko(), now=T + timedelta(hours=3))
        snapshot = store.events("holders", token=A)[0]
        assert [a.balance for a in snapshot.accounts] == [Decimal("5"), Decimal("4")]
        assert snapshot.complete is False and snapshot.total_supply is None
        assert result["tokens"][0]["holders"]["saturated"] is True
        evidence = store.get_state(f"enrichment:{A}")
        assert evidence["holders"]["raw_id"]
        assert evidence["holders"]["scope"] == "current_positive_token_balances"
        query, root, cube = provider.calls[0]
        assert (root, cube) == ("EVM", "Holders")
        assert "Balance" in query and "Amount" in query and 'gt:"0"' in query
        assert "count:3" in query


def test_funding_uses_earliest_valid_prior_usdc_transfer_in_bounded_window():
    from arc_monitor.enrichment import enrich_observed

    purchase = T + timedelta(hours=2)
    rows = [
        transfer(at=purchase - timedelta(hours=3)),
        transfer(at=purchase - timedelta(minutes=50), token=A),
        transfer(at=purchase - timedelta(minutes=45), native=True),
        transfer(at=purchase - timedelta(minutes=40), success=False),
        transfer(at=purchase - timedelta(minutes=30), amount="11"),
        transfer(at=purchase - timedelta(minutes=5), amount="12"),
        transfer(at=purchase + timedelta(minutes=1), amount="99"),
    ]
    provider = Provider([holder(W, "5")], rows)
    with observed_store() as store:
        result = enrich_observed(store, provider, Rpc(), funding_hours=1,
                                 now=T + timedelta(hours=3))
        funding = store.events("funding")
        assert len(funding) == 1
        assert funding[0].ts == purchase - timedelta(minutes=30)
        assert funding[0].amount_usdc == Decimal("11") and funding[0].funder == FUNDER
        summary = result["funding"]
        assert summary["wallets_considered"] == 1 and summary["observed"] == 1
        assert summary["rejected_rows"] == 5
        funding_query = next(query for query, _, cube in provider.calls if cube == "Transfers")
        assert "TransactionStatus" in funding_query and "Success" in funding_query
        assert "Native" in funding_query and USDC in funding_query


def test_contract_and_liquidity_are_current_and_unknown_risk_is_explicit():
    from arc_monitor.enrichment import enrich_observed

    at = T + timedelta(hours=3)
    with observed_store() as store:
        result = enrich_observed(store, Provider([holder(W, "5")], []), Rpc(),
                                 gecko=Gecko(), now=at, _clock=lambda: at)
        liquidity = store.events("liquidity", token=A)[0]
        risk = store.events("risk", token=A)[0]
        assert liquidity.ts == at and liquidity.amount_usdc == Decimal("1234.5")
        assert risk.ts == at
        assert risk.buy_tax_bps is risk.sell_tax_bps is risk.dangerous_permissions is risk.lp_locked is None
        assert "not a security audit" in risk.evidence.lower()
        contract = result["tokens"][0]["contract"]
        assert contract["block_number"] == 100 and contract["block_hash"] == "0x" + "c" * 64
        assert contract["code_sha256"] and contract["owner"] == W
        assert contract["total_supply"] == "1000000"
        assert contract["proxy"]["status"] == "no_known_slot_value"
        assert result["creator_history"][W]["observed_launches"] == 1
def test_amount_filter_uses_bitquery_decimal_string():
    from arc_monitor.enrichment import _holders_query, _funding_query
    from datetime import datetime, timezone, timedelta
    at = datetime(2026, 9, 22, tzinfo=timezone.utc)
    token = '0x' + '1' * 40
    assert 'gt:"0"' in _holders_query(token, 2)
    assert 'gt:"0"' in _funding_query(token, at-timedelta(hours=1), at)


def test_persistent_rotation_prevents_token_and_wallet_starvation():
    from arc_monitor.enrichment import enrich_observed

    tokens = ["0x" + format(i, "040x") for i in range(101, 104)]
    wallets = ["0x" + format(i, "040x") for i in range(201, 204)]
    at = T + timedelta(hours=3)
    with Store(":memory:", mode="demo") as store:
        store.ingest([
            *[Launch(id=f"l{i}", token=token, ts=T + timedelta(seconds=i), source="test",
                     tx="0x" + format(i, "064x"), pool=POOL)
              for i, token in enumerate(tokens)],
            *[Trade(id=f"t{i}", token=tokens[0], ts=T + timedelta(minutes=10 + i),
                    source="test", tx="0x" + format(20 + i, "064x"), wallet=wallet,
                    side="buy", quantity="1", quote_amount="1", pool=POOL)
              for i, wallet in enumerate(wallets)],
        ])
        provider = Provider([], [])
        token_order = []
        wallet_order = []
        for _ in range(4):
            result = enrich_observed(store, provider, Rpc(), max_tokens=1, max_wallets=1, now=at)
            token_order.append(result["tokens"][0]["token"])
            wallet_order.append(result["funding"]["queries"][0]["wallet"])
        assert token_order == [*tokens, tokens[0]]
        assert wallet_order == [*wallets, wallets[0]]
        rotation = store.get_state("enrichment:rotation")
        assert rotation["sequence"] == 8


def test_source_failures_do_not_discard_independent_successes():
    from arc_monitor.enrichment import enrich_observed
    from arc_monitor.providers.bitquery import ProviderError

    class HolderFailure(Provider):
        def execute(self, query, root, cube):
            if cube == "Holders":
                raise ProviderError("holder unavailable", retryable=True)
            return super().execute(query, root, cube)

    class GeckoFailure:
        def pool_liquidity(self, *args, **kwargs):
            raise ProviderError("pool missing")

    with observed_store() as store:
        result = enrich_observed(store, HolderFailure([], []), Rpc(), gecko=GeckoFailure(),
                                 now=T + timedelta(hours=3))
        token = result["tokens"][0]
        assert token["contract"]["status"] == "ok"
        assert token["holders"] == {"status": "failed", "source": "bitquery_holders",
                                    "error_type": "provider_error", "retryable": True}
        assert token["liquidity"]["status"] == "failed"
        assert store.count("risk") == 1 and store.count("liquidity") == 0
        assert store.get_state(f"enrichment:{A}")["contract"]["raw_id"]


def test_rpc_failure_still_persists_holders_and_gecko_liquidity():
    from arc_monitor.enrichment import enrich_observed
    from arc_monitor.providers.bitquery import ProviderError

    class RpcFailure(Rpc):
        def call(self, method, params=None):
            raise ProviderError("rpc unavailable", retryable=True)

    with observed_store() as store:
        result = enrich_observed(store, Provider([holder(W, "5")], []), RpcFailure(),
                                 gecko=Gecko(), now=T + timedelta(hours=3))
        token = result["tokens"][0]
        assert token["contract"]["status"] == "failed"
        assert token["holders"]["status"] == "ok"
        assert token["liquidity"]["status"] == "ok"
        assert store.count("holders") == store.count("liquidity") == store.count("risk") == 1


def test_rpc_chain_check_transport_failure_does_not_block_non_rpc_sources():
    from arc_monitor.enrichment import enrich_observed
    from arc_monitor.providers.bitquery import ProviderError

    class VerifyFailure(Rpc):
        def verify(self):
            raise ProviderError("rpc unavailable", retryable=True)

    with observed_store() as store:
        result = enrich_observed(store, Provider([holder(W, "5")], []), VerifyFailure(),
                                 gecko=Gecko(), now=T + timedelta(hours=3))
        assert result["rpc"] == {"status": "failed", "source": "rpc_chain",
                                 "error_type": "provider_error", "retryable": True}
        assert result["tokens"][0]["contract"]["status"] == "failed"
        assert result["tokens"][0]["holders"]["status"] == "ok"
        assert result["tokens"][0]["liquidity"]["status"] == "ok"


def test_budget_exhaustion_propagates_after_already_completed_source_is_saved():
    import pytest
    from arc_monitor.budget import BudgetExceeded
    from arc_monitor.enrichment import enrich_observed

    class Exhausted(Provider):
        def execute(self, query, root, cube):
            raise BudgetExceeded("stop")

    with observed_store() as store:
        with pytest.raises(BudgetExceeded):
            enrich_observed(store, Exhausted([], []), Rpc(), now=T + timedelta(hours=3))
        assert store.count("risk") == 1
        assert store.get_state(f"enrichment:{A}")["contract"]["status"] == "ok"


def test_uninjected_current_snapshot_time_is_after_source_completion():
    from arc_monitor.enrichment import enrich_observed

    class TimedProvider(Provider):
        completed_at = None

        def execute(self, query, root, cube):
            result = super().execute(query, root, cube)
            if cube == "Holders":
                self.completed_at = datetime.now(timezone.utc)
            return result

    provider = TimedProvider([holder(W, "5")], [])
    with observed_store() as store:
        enrich_observed(store, provider, Rpc(), gecko=None)
        assert store.events("holders", token=A)[0].ts >= provider.completed_at


def test_public_now_is_selection_cutoff_and_cannot_backdate_current_snapshots():
    from arc_monitor.enrichment import enrich_observed

    before = datetime.now(timezone.utc)
    cutoff = T + timedelta(hours=3)
    with observed_store() as store:
        enrich_observed(store, Provider([holder(W, "5")], []), Rpc(), now=cutoff)
        assert store.events("holders", token=A)[0].ts >= before
        assert store.events("risk", token=A)[0].ts >= before


def test_future_launch_and_trade_are_excluded_by_selection_cutoff():
    from arc_monitor.enrichment import enrich_observed

    cutoff = T + timedelta(hours=3)
    future_token = "0x" + "6" * 40
    future_creator = "0x" + "4" * 40
    with observed_store() as store:
        store.ingest([
            Launch(id="future-launch", token=future_token, ts=cutoff + timedelta(seconds=1),
                   source="test", tx="0x" + "d" * 64, pool=POOL, creator=future_creator),
            Trade(id="future-buy", token=future_token, ts=cutoff + timedelta(seconds=1),
                  source="test", tx="0x" + "e" * 64, wallet="0x" + "5" * 40,
                  side="buy", quantity="1", quote_amount="1", pool=POOL),
        ])
        result = enrich_observed(store, Provider([], []), Rpc(), max_tokens=10,
                                 max_wallets=10, now=cutoff)
        assert [item["token"] for item in result["tokens"]] == [A]
        assert result["funding"]["wallets_available"] == 1
        assert future_creator not in result["creator_history"]
