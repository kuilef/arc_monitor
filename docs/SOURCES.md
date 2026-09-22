# Sources checked for implementation — 22 September 2026

These are external API references, not proof that an authenticated query or a full-market
backfill succeeded in this delivery. No Bitquery token was available.

- Arc overview and network identifiers: https://docs.bitquery.io/docs/blockchain/arc-mainnet/
- Trades, recent rolling window, pair orientation and v4 Pool.Id:
  https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-trades-api/
- Registry signatures and token topic positions:
  https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-launchpads-api/
- Argus early undecoded events, raw topics, fees and creator fields:
  https://docs.bitquery.io/docs/blockchain/arc-mainnet/argus-launchpad-api/
- Minara strategy versus shared factory, finalPositionRecipient versus creator,
  provider-specific log indexes:
  https://docs.bitquery.io/docs/blockchain/arc-mainnet/minara-launchpad-api/
- Holder/balance availability: https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-balances-api/
- Trading row identities and duplicate limitations:
  https://docs.bitquery.io/docs/trading/crypto-trades-api/trades-api/
- GeckoTerminal public request limits and reserve/FDV meanings: https://apiguide.geckoterminal.com/faq
- GitHub integration permissions diagnosis:
  https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api#resource-not-accessible

## Material differences from the aspirational input brief

1. Trading has a rolling recent window; full archived history cannot be assumed.
   The implementation does not send archive/combined arguments to Trading.
2. A decoded event name alone misses early Argus rows. Indexed raw topics are used.
3. Transaction sender is not generally the creator. Minara topic 3 is an LP recipient;
   its creator is unknown here until a proper correlated event is supplied.
4. Trading rows lack a universally reliable swap-log identity in the selected schema.
   Fingerprints remove exact duplicates but may conflate identical genuine legs.
   Exact raw-log reconciliation is a remaining integration requirement.
5. RPC logIndex and Bitquery LogHeader.Index are not assumed interchangeable.
6. Native and ERC-20 USDC views are not added. Quote identity is verified by identifier/address,
   not ticker; reserve USD estimates are separately marked as assuming a USDC peg.
7. Same funder is a proxy, not proof of common ownership. Exchange/service hubs must be tagged.
8. MessageReceived alone is not a USDC inflow amount. Only separately reconciled Flow events
   are accepted as CCTP inputs; automatic reconciliation is not implemented.
9. Investor-return labels need executable prices and costs. The output here is explicitly
   event-time price research, not a trading-strategy backtest.

The initial source specified three stages: research, monitoring, then alerts. This delivery
implements the reusable data/analysis engine and a runnable local monitoring interface.
It does not claim that the initial full-market research or predictive validation has happened.
