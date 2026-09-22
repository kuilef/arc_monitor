"""CLI orchestration for metered enrichment and independent RPC evidence."""

import os
from contextlib import ExitStack

from .budget import RequestBudget
from .monitoring import CollectionSession
from .operations import WriterLock
from .providers.rpc import RpcClient
from .providers.gecko import GeckoClient
from .storage import Store


def rpc_client(args):
    return RpcClient(
        args.rpc_url,
        budget=RequestBudget(
            max_requests=args.rpc_max_requests,
            rpm=60,
            points_budget=args.rpc_max_requests,
            points_per_request=1,
            reserve_points=0,
        ),
    )


def run_evidence(args, emit):
    if args.mode != "live":
        raise ValueError("External evidence requires a separate live database")
    with ExitStack() as stack:
        stack.enter_context(WriterLock(args.db))
        store = stack.enter_context(Store(args.db))
        if args.command == "reconcile":
            from .reconciliation import reconcile_trade

            if not 1 <= args.limit <= 100:
                raise ValueError("Reconciliation limit must be 1..100")
            rpc = stack.enter_context(rpc_client(args))
            trades = store.events("trade")
            selected = (
                [t for t in trades if t.id == args.trade_id] if args.trade_id else trades[-args.limit :]
            )
            if args.trade_id and not selected:
                raise ValueError("Stored trade ID was not found")
            for trade in selected:
                emit(reconcile_trade(store, rpc, trade))
            return 0
        session = stack.enter_context(
            CollectionSession(
                store,
                os.getenv("BITQUERY_TOKEN", ""),
                max_requests=args.max_requests,
                rpm=args.rpm,
                points_budget=args.points_budget,
                daily_points=args.daily_points,
                points_per_request=args.points_per_request,
                reserve_points=args.reserve_points,
                state_key="operation:" + args.command,
            )
        )
        emit({"quota": session.refresh_usage()})
        if args.command == "quota":
            return 0
        if args.command == "probe-history":
            from .history import probe_history

            emit(probe_history(store, session.provider, args.dates, window_seconds=args.window_seconds))
            return 0
        rpc = stack.enter_context(rpc_client(args))
        if args.command == "enrich":
            from .enrichment import enrich_observed

            gecko = stack.enter_context(
                GeckoClient(
                    budget=RequestBudget(max_requests=20, rpm=20, points_budget=20, points_per_request=1)
                )
            )
            result = enrich_observed(
                store,
                session.provider,
                rpc,
                max_tokens=args.max_tokens,
                max_wallets=args.max_wallets,
                holder_limit=args.holder_limit,
                funding_hours=args.funding_hours,
                gecko=gecko,
            )
            store.set_state("enrichment", result)
            emit(result)
        elif args.command == "cctp":
            from .cctp import collect_cctp

            emit(
                collect_cctp(
                    store,
                    session.provider,
                    rpc,
                    args.start,
                    args.end,
                    limit=args.limit,
                    max_transactions=args.max_transactions,
                )
            )
    return 0
