# CCTP reconciliation on Arc mainnet

`collect_cctp(store, provider, rpc, start, end, *, limit=100, max_transactions=20)`
adds only receipt-reconciled CCTP V2 USDC flows. The interval is whole-second,
half-open UTC `[start,end)`. Bitquery discovers candidate transactions; Arc RPC is
the source for successful receipts, canonical block identity, time, protocol logs,
and token transfers.

## Fixed mainnet scope

The collector is pinned to Arc chain ID 5042 and CCTP domain 26:

- TokenMessengerV2: `0x28b5a0e9C621a5BadaA536219b3a228C8168cf5d`
- MessageTransmitterV2: `0x81D40F21F12A8F0E3252Bccb954D722d4c464B64`
- TokenMinterV2: `0xfd78EE919681417d192449715b2594ab58f5D002`
- USDC ERC-20 interface: `0x3600000000000000000000000000000000000000`, 6 decimals

Addresses and domain are from the [Arc contract-address reference](https://docs.arc.io/arc/references/contract-addresses).
The message header and events follow Circle's
[MessageTransmitterV2](https://github.com/circlefin/evm-cctp-contracts/blob/master/src/v2/MessageTransmitterV2.sol),
[TokenMessengerV2](https://github.com/circlefin/evm-cctp-contracts/blob/master/src/v2/TokenMessengerV2.sol),
[BaseTokenMessenger](https://github.com/circlefin/evm-cctp-contracts/blob/master/src/v2/BaseTokenMessenger.sol),
[MessageV2](https://github.com/circlefin/evm-cctp-contracts/blob/master/src/messages/v2/MessageV2.sol),
and [BurnMessageV2](https://github.com/circlefin/evm-cctp-contracts/blob/master/src/messages/v2/BurnMessageV2.sol).

## Acceptance rules

An outgoing flow requires exactly one canonical `MessageSent`, exactly one
`DepositForBurn`, agreement of all routing, token, amount, sender, fee, and finality
fields, and a canonical USDC `Transfer` from TokenMinterV2 to the zero address for
the same amount. Its amount is the source burn amount.

An incoming flow requires exactly one canonical `MessageReceived`, exactly one
`MintAndWithdraw`, a valid V2 burn body, `burn amount - fee == minted amount`, and a
canonical USDC `Transfer` from the zero address to the mint recipient for that net
amount. When the executed fee is nonzero, it also requires the distinct canonical
USDC mint `Transfer` for that fee. The stored incoming Flow amount is the gross
minted supply (`recipient net + fee`), matching the outgoing gross burn convention.
`MessageReceived` alone never supplies an amount.

Finalized incoming identity is
`cctp-v2:<source-domain>:<32-byte-nonce>`. Circle's V2 `MessageSent` source payload
contains a zero nonce placeholder; Iris assigns the final nonce off-chain. Therefore
an Arc outgoing source observation uses the explicitly pending identity
`cctp-v2-pending:26:<source-transaction>:<MessageSent-log-index>`. It is not treated
as the finalized nonce representation. A repeated finalized nonce, inconsistent
Bitquery/RPC direction, fake emitter, unsuccessful receipt, block mismatch, wrong
domain, or multiple canonical message events makes that candidate unmatched.

## Provenance and coverage

Every discovery batch, receipt, and canonical block is saved in `raw_batches`.
Rejected candidates are also saved with a bounded reason and receipt raw ID. Valid
flows may be stored even when another candidate fails, but `flows` coverage is
recorded only if every discovered candidate resolves. A complete empty discovery
window is deliberately recorded as covered.

Every accepted Flow also has a direct proof record at Store state key
`cctp:<FlowID>`. It links the Flow to the source transaction, receipt and block raw
IDs, canonical block hash/time, message event index, paired protocol event index,
and USDC mint/burn event indexes. The same proof is saved in `raw_batches` as
`cctp_provenance` with the Flow ID. Incoming proof records gross, recipient-net,
executed-fee, and maximum-fee base units. Outgoing proof leaves recipient net and
executed fee null because a source receipt proves only the gross burn and maximum
permitted destination fee.

Discovery splits saturated intervals until one second. A still-saturated one-second
interval is saved with `saturated_one_second_window` and receives no coverage. The
transaction cap prevents an unexpectedly large candidate set from consuming an
unbounded number of RPC calls.

## Limits

The implementation intentionally supports only receipts with one CCTP message.
Multi-message receipts remain raw and unmatched. It does not consult Circle's
attestation API, prove Bitquery index completeness independently, resolve remote
token registries off-chain, or turn successful unit tests into a claim of complete
mainnet history. Live Bitquery schema and provider-budget behavior require a
credentialed integration run before operational use.
