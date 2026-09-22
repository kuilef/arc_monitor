# Bounded enrichment and receipt reconciliation

`enrich_observed(store, provider, rpc, *, max_tokens=3, max_wallets=10,
holder_limit=1000, funding_hours=24, gecko=None, now=None)` enriches only tokens and
buyers already present in the event store. It does not discover the wider market. The
default upper bound is three holder calls, ten funding calls, and the pinned read-only RPC
checks for those three contracts. Every Bitquery request still passes through
`provider.execute(...)`, so the provider's HTTP quota and retry accounting remain active.
`now` is only an event-selection cutoff: launches and trades after it are excluded. It never
sets a current holder, risk, or liquidity timestamp. Current evidence is stamped after its
source call completes; tests can inject the private `_clock` hook for deterministic time.

Bounded periodic runs use the persistent `enrichment:rotation` state. Tokens and buyer
wallets with the oldest attempt sequence are selected first, including wallets across the
whole eligible observed trade set. Failed attempts also rotate, so one unavailable source
cannot make newer entities wait forever.

## Evidence written

- Current holders use Arc `EVM.Holders`, filter the requested contract and positive
  `Balance.Amount`, order by balance, and request `holder_limit + 1`. The extra row is a
  truncation sentinel. `Holder.Address`, `Balance.Amount`, `FirstChangeTime`, and
  `LastChangeTime` are retained in the raw batch. The normalized snapshot is stamped at
  the actual observation time; it is never backdated to a launch or trade.
- Holder snapshots are incomplete by default. A response shorter than the sentinel still
  lacks a holder-state block in the documented response, so it cannot be proven consistent
  with the RPC `totalSupply` block. No supply denominator is attached in that case. The
  evidence report distinguishes this condition from limit saturation.
- Funding queries are per selected buyer and use the explicit half-open interval
  `[first purchase - funding_hours, first purchase)`. Only successful, incoming, non-native
  ERC20 USDC rows with six decimals are accepted. The earliest valid row in that bounded
  response is stored. It means "earliest observed in this query window", never first-ever
  funding. A 100-row response is marked saturated.
- Contract evidence pins block number, block hash, timestamp, bytecode SHA-256,
  `decimals`, `totalSupply`, `owner()`, and the EIP-1967 implementation slot to one block.
  Unsupported calls remain unknown. A `Risk` event keeps taxes, dangerous permissions,
  LP lock, and both tax rates null. Interface and storage checks are expressly not a
  contract security audit.
- Liquidity is recorded only for a pool already attached to an observed launch and only
  when an injected current-data source is present. GeckoTerminal reserve USD is labelled
  `usd_assuming_usdc_peg`. A 32-byte v4 pool id must never be replaced by the shared
  PoolManager address or an aggregate manager balance.
- Creator history counts launches already observed in the same store and is labelled
  incomplete. It is not presented as complete chain history.

Raw batches are stored with `Store.save_raw`. Structured provenance is available at
`enrichment:<token>`, `enrichment:funding`, and `enrichment:creator_history` through
`Store.get_state`. RPC chain/contract, holders, liquidity, and each funding query have typed
`ok`, `partial`, `failed`, or `skipped` status evidence. A provider, RPC, or Gecko failure is
recorded without discarding evidence already completed by another source. Budget exhaustion
is not converted to a source failure: it propagates and stops further requests.

## Receipt reconciliation

`reconcile_trade(store, rpc, trade)` preserves the original normalized trade and writes a
separate report at `reconciliation:<trade.id>`. The receipt is stored independently as raw
RPC evidence. The function verifies Arc chain identity, successful receipt status,
transaction hash, receipt/block number and hash, block timestamp, and token decimals at the
receipt block. Every receipt log must explicitly be non-removed and carry the same
transaction hash, block hash, and block number as that verified receipt. Missing identity,
cross-receipt logs, and removed logs leave the report `unknown` before transfer parsing.

For an address-based pool, a buy requires exactly one token `Transfer` from pool to buyer
and one USDC `Transfer` from buyer to pool; sell directions are reversed. Amounts are
compared as integer base units. The report retains both log indices and the difference
introduced when a normalized decimal cannot map to integer units. Multiple matching legs,
missing legs, unresolved pool identity, or unavailable decimals stay `unknown`; a unique
pair with different integer amounts is `mismatch`. Reconciliation never inserts a second
trade event.

For a 32-byte v4 pool id, manager resolution requires one stored raw canonical Uniswap v4
`Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)` event. The pool id
must occupy indexed topic 1, its two indexed currencies must be the trade token and USDC,
and the raw record must retain its source batch, transaction hash, block number, provider
log index, non-removed flag, and five-word ABI data. The trade receipt must also contain
exactly one canonical v4 `Swap` for that pool id from the same emitter. Both initialization
and swap provenance are retained in `pool_context`. Missing, ambiguous, generic topic, or
transfer-only evidence remains `unknown`; reconciliation does not infer a PoolManager from
an arbitrary emitter or from generic manager transfers.

Primary schema references:

- [Arc balances API](https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-balances-api/)
- [Arc transfers API](https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-transfers-api/)
- [Uniswap v4 PoolManager interface](https://github.com/Uniswap/v4-core/blob/main/src/interfaces/IPoolManager.sol)

Unit tests use synthetic provider and RPC responses. They do not consume a live key or make
network requests.
