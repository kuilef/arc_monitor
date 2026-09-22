# Bounded history availability probes

`probe_history(store, provider, dates, *, window_seconds=120, include_flows=True)`
checks up to eight short, past UTC windows. It issues exactly one `limit=1` query
per selected stream and date:

- `launches` through `provider.fetch("launches", start, end, 1)`
- `trades` through `provider.fetch("trades", start, end, 1)`
- `flows` through the bounded CCTP Events query when `include_flows=True`

All input is validated before the first HTTP request. Each response or provider
error is saved in `raw_batches` as `history_probe` evidence.

## Interpreting results

`available_if_rows` means at least one row was returned under the exact query at
that date. It proves only availability of that sample. Because the query is capped
at one row, it cannot establish event counts, completeness, continuous retention,
or the earliest available date.

`empty_not_proof_absent` means the provider returned an empty list. It does not
distinguish a genuinely event-free interval from plan, retention, index, or query
scope limits. `provider_error` preserves the provider failure and retryability flag;
it is never converted into an empty result. A request or point `BudgetExceeded`
exception propagates and stops the probe immediately.

The probe never normalizes or ingests returned rows and never writes collection
coverage or checkpoints.

## Sources outside this probe

Holder and liquidity adapters expose current snapshots; they do not prove
historical depth. RPC contract inspection is also current-state inspection unless
an explicitly pinned historical block is queried and supported by the endpoint.

Funding evidence is transaction history scoped to a particular wallet. Its depth
must be probed per wallet and query scope. A successful sample for one wallet does
not establish global funding-history availability.

Live retention and plan behavior require credentialed provider checks. Unit tests
use synthetic responses and make no live-history claim.
