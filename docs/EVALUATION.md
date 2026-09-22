# Independent research evaluation

`arc_monitor.evaluation.evaluate_research` evaluates the monitor's fixed launch
rules against the existing activity-based `outcome(...).survived` label. It does
not fit a price model and does not use the future maximum-price fields as its
target.

## Cohort and hold-out protocol

Only `launch_event` origins enter the cohort. The default row uses features 15
minutes after launch and the one-hour survival outcome. A row is eligible only
when trade coverage is continuous from launch through the feature cutoff and
through the label horizon. Incomplete or immature rows remain in the report as
censored counts; they are not silently treated as failures.

Training labels must end at or before the split timestamp. Test features must
start at or after it. Rows whose label windows cross the split are purged. Any
token already present in training is also removed from the test partition. When
`split_at` is omitted, the evaluator chooses a chronological boundary nearest a
70% training share that still meets `min_train` and `min_test`, if such a
boundary exists. The current sparse live database is expected to return
`status: insufficient_data`, not an apparent performance estimate.

The fixed candidates are:

- `runner_65`: runner score at least 65;
- `buyers_10`: at least ten buyer-cluster proxies;
- `volume_1000`: adjusted observed volume at least 1,000 USDC;
- pairwise conjunctions of those signals, reported as ablations.

Selection uses training precision, then recall, then the lower false-alert
count. Stable candidate order resolves an exact tie. Only the selected rule is
evaluated on the held-out partition. The report includes the confusion matrix,
precision, recall, false-positive rate, false-alert count, eligible coverage,
and censor reasons. `selection.parameters` records the stable feature/operator/
value definition, and downstream notification code can call
`matches_rule(selection.rule, features)` to apply that exact rule. A funding
cluster is still only a proxy; it is not a count of independent people.

## Conditional execution scenario

For held-out alerts, entry is the first observed eligible trade at or after the
configured alert latency. Wash diagnostics for that decision use only the event
prefix available at the candidate entry time; later round trips cannot change an
earlier decision. Exit is the last observed eligible trade in the final 15
minutes before the label horizon, never the best future price. Both entry and
exit require a fresh historical liquidity snapshot. The position may not exceed
1% of the lower of those two observed liquidity values. Fees and slippage are
applied on both sides. Known historical buy and sell taxes are also applied.

This is a conditional replay. Trade prints do not prove that an order of the
modeled size could fill at that price, and snapshots do not prove that a sale
would succeed. Tax, dangerous-permission, and LP evidence must have a source and
be no older than 15 minutes at both entry and exit. Missing evidence is reported
as `tax_unknown`, `sellability_unknown`, or `lp_status_unknown`. Even complete
dangerous-permission evidence does not prove sale success. The field
`verified_executable_pnl_usdc` is therefore `null`; modeled values appear only
under `conditional_pnl_usdc`.

## Notification gate and artifacts

The notification gate is false when independent training or test samples are
below the configured minima or the absolute 10/10 floor, held-out precision is below 0.60, eligible
coverage is below 0.80, the test rule emits no alerts, or executable PnL remains
unverified. Passing the gate is permission for downstream code to consider the
saved evaluation; it is not proof of profit or permission to trade.

Each report records:

- protocol identifier `arc-evaluation-v1`;
- SHA-256 of the complete evaluation configuration;
- SHA-256 of ordered historical events, covered intervals, and immutable dataset settings;
- SHA-256 of the complete evaluation report content;
- a gate SHA-256 binding the protocol, configuration, data, and report fingerprints.

`dataset_fingerprint(store, observed_until)` binds ordered historical events,
coverage intervals clipped to the cutoff, and immutable dataset settings. It
intentionally excludes operational settings such as collector, quota, and saved
evaluation state so persisting a gate does not invalidate it. Notification code
must recompute and compare this data fingerprint and use
`verify_report_fingerprints(report)` before trusting a saved report.

The output directory receives atomically replaced `evaluation.json` and
`evaluation.csv`. CSV cells beginning with spreadsheet formula prefixes are
apostrophe-escaped. The CSV contains partition, label, prediction, and
conditional execution fields for audit; JSON is the authoritative structured
report.

## Verification limits

Results describe only covered provider windows and the events stored locally.
They do not establish all-chain completeness, causal predictive power, unique
human buyers, market-wide liquidity, tax certainty without historical evidence,
sellability, MEV conditions, API-delivery latency, or executable returns. Under
the current observation-only protocol, executable PnL remains unverified and
the notification gate therefore stays closed.
Repeated evaluation or threshold changes after seeing hold-out results invalidate
the hold-out and require a newly collected test period with a new fingerprint.
