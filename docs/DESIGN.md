# Arc Monitor — design and explicit boundaries

Date: 2026-09-22. Basis: the supplied 24-section Arc-monitor brief.

Goal: a read-only, single-user research pipeline for Arc launches, buyer growth,
liquidity and concentration, followed by a monitor. No wallet signing or trading.

## Architecture

Provider HTTP/RPC -> immutable normalized events + original response batches ->
SQLAlchemy storage -> event-time metrics -> research export / FastAPI dashboard.
SQLite is the zero-service local default; PostgreSQL is an optional deployment target.
This differs from the brief's PostgreSQL-first deployment, not its analytical model.
All monetary input is Decimal, persisted as decimal text inside JSON, never a SQL float.

The database is bound to one dataset mode (demo/live) and chain ID. Demo is visibly
synthetic, deterministic and prohibited from sharing a database with live records.
Unseen contract addresses discovered through USDC trades are first-seen discoveries,
NOT automatically launches. V4 pool IDs are not the shared PoolManager address.

## Availability / time

All intervals are half-open UTC [start,end). Features use only event timestamps < cutoff.
Historical replay is event-time replay, NOT a claim to reproduce vendor delivery latency.
Coverage means a fully fetched provider interval under the configured query scope,
NOT independently proven completeness of the chain index.
A capped interval is split; a still-capped one-second interval fails explicitly.
Partial/failed intervals never advance their cursor. Repeated ingestion is idempotent.

Unknown fields remain null. No present-day holder/liquidity state is backdated.
Incomplete or immature outcomes are censored (null), not failed tokens.

## Analytical honesty

Adjusted volume is volume remaining after explicit heuristics, not proven real demand.
First-observed-funder clusters are a proxy, not identity resolution or independent people.
Shared exchange/bridge funding is optionally excluded from clustering.
Gross/net volume alone does not establish wash trading. Round-trip heuristics can flag
legitimate arbitrage. Scores are versioned, hand-written and not calibrated probabilities.
Risk Score is withheld when required risk evidence is missing; an observed component
score and coverage are displayed separately. Unknown is never presented as safe.
FDV is not market cap. Price multiple is not executable return; slippage, liquidity,
transfer tax and MEV are not simulated by a simple historical price label.

## Deliverable boundary

Implement local storage, normalized imports, Bitquery HTTP backfill + resumed polling,
known launch decoders with raw-topic fallback, trade-based generic discovery,
read-only RPC chain/contract verification and launch-log scanning, optional GeckoTerminal
liquidity snapshots, metrics, creator history, survival/price-multiple labels,
research exports, offline browser UI, deduplicated local alerts, tests and run scripts.

Live authentication and full-history vendor responses cannot be verified without an API key.
No key was supplied. Full transfer-graph reconstruction, audited contract-risk analysis,
CCTP message reconciliation, automatic holder-history collection, websocket ingestion,
Parquet archiving, trained prediction models and automatic Telegram delivery remain
explicit extensions, not hidden stub implementations. Normalized holder/funding/flow/risk
imports permit those facts to be analyzed now when a verified upstream produces them.

## Execution ruling

The user explicitly requested planning, implementation, testing and continuation now.
Proceed inline rather than inserting another approval cycle. GitHub write was denied
403 by the integration; use a new isolated local git repository and ship a complete ZIP.
