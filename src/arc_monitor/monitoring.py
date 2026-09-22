"""Bounded resumable collection with a persistent ledger and quota reservations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import math
import time
import uuid

from .budget import RequestBudget, BudgetExceeded
from .collection import collect
from .models import iso, parse_time
from .operations import WriterLock, coverage_intervals
from .providers.bitquery import BitqueryClient, ProviderError
from .storage import Store


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


class CollectionSession:
    def __init__(
        self,
        store,
        token,
        *,
        max_requests=100,
        rpm=6,
        points_budget=100,
        daily_points=200,
        points_per_request=10,
        reserve_points=20,
        client=None,
        sleep=time.sleep,
        state_key="collector",
    ):
        self.store = store
        self.state_key = state_key
        if not math.isfinite(daily_points) or daily_points <= 0:
            raise ValueError("Daily point budget must be finite and positive")
        self.daily_points = daily_points
        self.last_reserved = 0
        self.account_meter = None
        self.token_expires = None
        raw_expiry = os.getenv("ARC_TOKEN_EXPIRES_AT")
        if raw_expiry:
            try:
                self.token_expires = parse_time(raw_expiry)
            except ValueError:
                raise ValueError("ARC_TOKEN_EXPIRES_AT must be an aware ISO timestamp") from None
        self.run_id = uuid.uuid4().hex
        self.state = {
            "operation": state_key,
            "run_id": self.run_id,
            "started_at": iso(now()),
            "heartbeat_at": iso(now()),
            "status": "starting",
            "error_code": None,
        }
        self.budget = RequestBudget(
            max_requests=max_requests,
            rpm=rpm,
            points_budget=points_budget,
            points_per_request=points_per_request,
            reserve_points=reserve_points,
            sleep=sleep,
            on_change=self._reserve,
        )
        self.provider = BitqueryClient(
            token, max_requests=max_requests, budget=self.budget, client=client, sleep=sleep
        )

    def _reserve(self, summary):
        if self.token_expires and self.token_expires <= now() + timedelta(minutes=5):
            raise BudgetExceeded("Configured API token expiry is reached or within five minutes")
        day = now().date().isoformat()
        daily = self.store.get_state("daily_budget") or {"day": day, "reserved_points": 0}
        if daily["day"] != day:
            daily = {"day": day, "reserved_points": 0}
        additional = summary["reserved_points"] - self.last_reserved
        if daily["reserved_points"] + additional > self.daily_points:
            raise BudgetExceeded("Persistent daily point reservation budget exhausted")
        updates = {}
        if self.account_meter is not None and additional:
            projected = max(
                self.account_meter["observed_used"],
                self.account_meter["baseline_used"] + self.account_meter["reserved_points"] + additional,
            )
            if self.account_meter["limit"] - projected < self.budget.reserve_points:
                raise BudgetExceeded("Persistent account point reserve reached")
            self.account_meter["reserved_points"] += additional
            updates["account_budget"] = self.account_meter
        daily["reserved_points"] += additional
        updates["daily_budget"] = daily
        self.store.set_states(updates)
        self.last_reserved = summary["reserved_points"]
        self.update("running", budget=summary)

    def update(self, status, **fields):
        self.state.update(status=status, heartbeat_at=iso(now()), **fields)
        self.store.set_state(self.state_key, self.state)

    def refresh_usage(self):
        rejection = None
        try:
            usage = self.provider.usage()
        except BudgetExceeded as error:
            usage = getattr(error, "usage", None)
            if usage is None:
                raise
            rejection = error
            if self.budget.last_usage is not usage:
                # Inactive or malformed billing response: retain the fresh
                # sanitized evidence without treating it as an active meter.
                self._save_usage({"checked_at": iso(now()), **usage, "validation_error": str(error)})
                raise
        period = usage["billing_period"]
        period_key = period.get("started_at") or "unknown-period"
        meter = self.store.get_state("account_budget")
        if not meter or meter["period"] != period_key:
            # First migration conservatively includes already persisted reservations.
            prior = self.store.get_state("daily_budget") or {}
            meter = {
                "period": period_key,
                "baseline_used": float(period["usage"]["points_usage"]),
                "reserved_points": prior.get("reserved_points", 0) if not meter else 0,
            }
        meter.update(
            observed_used=float(period["usage"]["points_usage"]),
            limit=float(period["limits"]["points_limit"]),
        )
        self.account_meter = meter
        self.store.set_state("account_budget", meter)
        remaining = meter["limit"] - max(
            meter["observed_used"], meter["baseline_used"] + meter["reserved_points"]
        )
        self.budget.account_remaining = min(self.budget.account_remaining, remaining)
        value = {
            "checked_at": iso(now()),
            **usage,
            "local_budget": self.budget.summary(),
            "persistent_account_budget": meter,
            "token_expiry": iso(self.token_expires) if self.token_expires else None,
            "token_expiry_note": "Operator-configured expiry when supplied; billing period end is not token expiry.",
        }
        self._save_usage(value)
        if rejection:
            raise rejection
        return value

    def _save_usage(self, value):
        self.store.set_state("quota", value)
        history = self.store.get_state("quota_history") or []
        history.append(value)
        self.store.set_state("quota_history", history[-200:])
        self.store.save_raw("bitquery_usage", value)

    def __enter__(self):
        previous = self.store.get_state(self.state_key)
        if previous and previous.get("status") in {"running", "waiting", "starting", "retrying"}:
            self.store.save_raw(
                "collector_run",
                {
                    **previous,
                    "status": "abandoned",
                    "recovery_observed_at": iso(now()),
                    "error_code": "previous_process_lost",
                },
            )
        self.update("starting")
        self.store.save_raw("collector_run", self.state)
        return self

    def __exit__(self, typ, error, tb):
        status = (
            "completed"
            if typ is None
            else "budget_exhausted"
            if isinstance(error, BudgetExceeded)
            else ("interrupted" if typ is KeyboardInterrupt else "failed")
        )
        self.update(
            status,
            finished_at=iso(now()),
            error_code=typ.__name__ if typ else None,
            stop_reason=str(error) if isinstance(error, (BudgetExceeded, ProviderError)) else None,
            budget=self.budget.summary(),
        )
        self.store.save_raw("collector_run", self.state)
        self.provider.__exit__(typ, error, tb)


def run_collection(args, token, emit):
    if args.mode != "live":
        raise ValueError("Managed collection requires --mode live")
    if args.command == "poll" and (
        args.interval < 1
        or args.lookback_minutes < 1
        or args.lag_seconds < 0
        or args.cycles < 0
        or args.overlap_seconds < 0
        or args.enrich_every < 0
        or args.max_failures < 0
    ):
        raise ValueError("Invalid polling interval, lookback, lag or cycles")
    with (
        WriterLock(args.db),
        Store(args.db) as store,
        CollectionSession(
            store,
            token,
            max_requests=args.max_requests,
            rpm=args.rpm,
            points_budget=args.points_budget,
            daily_points=args.daily_points,
            points_per_request=args.points_per_request,
            reserve_points=args.reserve_points,
        ) as session,
    ):
        emit({"quota": session.refresh_usage()})
        if args.command == "backfill":
            for kind in ["launches", "trades"] if args.stream == "both" else [args.stream]:
                result = collect(
                    store,
                    session.provider,
                    kind,
                    args.start,
                    args.end,
                    limit=args.limit,
                    max_requests=args.max_requests,
                    refresh=args.refresh,
                    window_seconds=args.window_seconds,
                )
                emit({"stream": kind, **result, "budget": session.budget.summary()})
            return 0
        cycles = 0
        failures = 0
        while True:
            end = now() - timedelta(seconds=args.lag_seconds)
            origin = store.get_state("monitor_start")
            if not origin:
                origin = iso(end - timedelta(minutes=args.lookback_minutes))
                store.set_state("monitor_start", origin)
            start = parse_time(origin)
            try:
                if cycles and cycles % 5 == 0:
                    session.refresh_usage()
                for kind in ["launches", "trades"]:
                    # Capture the newest window first even after a long outage.
                    # Historical repair cannot consume the entire budget before fresh data.
                    recent = max(
                        start, end - timedelta(seconds=max(args.lookback_minutes * 60, args.overlap_seconds))
                    )
                    result = collect(
                        store,
                        session.provider,
                        kind,
                        recent,
                        end,
                        limit=args.limit,
                        max_requests=args.max_requests,
                        refresh=True,
                        window_seconds=args.window_seconds,
                    )
                    emit({"cycle": cycles + 1, "stream": kind, "operation": "fresh", **result})
                for kind in ["launches", "trades"]:
                    gaps = coverage_intervals(store, kind, start, end)["gaps"] if start < end else []
                    # Repair one old chunk per stream/cycle; remaining gaps stay visible.
                    for gap in gaps[:1]:
                        left = parse_time(gap["start"])
                        right = min(parse_time(gap["end"]), left + timedelta(seconds=args.window_seconds))
                        result = collect(
                            store,
                            session.provider,
                            kind,
                            left,
                            right,
                            limit=args.limit,
                            max_requests=args.max_requests,
                            window_seconds=args.window_seconds,
                        )
                        emit({"cycle": cycles + 1, "stream": kind, "operation": "gap_fill", **result})
                if args.enrich_every and cycles % args.enrich_every == 0:
                    from .commands import rpc_client
                    from .enrichment import enrich_observed
                    from .providers.gecko import GeckoClient

                    with (
                        rpc_client(args) as rpc,
                        GeckoClient(budget=RequestBudget(max_requests=20, rpm=20, points_budget=20)) as gecko,
                    ):
                        result = enrich_observed(
                            store,
                            session.provider,
                            rpc,
                            max_tokens=args.max_tokens,
                            max_wallets=args.max_wallets,
                            gecko=gecko,
                        )
                        store.set_state("enrichment", result)
                        emit({"enrichment": result})
                if args.cctp:
                    from .commands import rpc_client
                    from .cctp import collect_cctp

                    with rpc_client(args) as rpc:
                        left = store.checkpoint("flows") or start
                        if left < end:
                            emit({"cctp": collect_cctp(store, session.provider, rpc, left, end)})
                if getattr(args, "telegram", False):
                    from .notifications import notify

                    emit({"notifications": notify(store, now(), send=True)})
                failures = 0
            except ProviderError as error:
                if not error.retryable or failures >= args.max_failures:
                    raise
                failures += 1
                session.update("retrying", error_code="provider_temporary_failure", retry_count=failures)
                time.sleep(min(60, 5 * 2 ** (failures - 1)))
                continue
            cycles += 1
            session.update(
                "waiting",
                cycles=cycles,
                budget=session.budget.summary(),
                next_wake_at=iso(now() + timedelta(seconds=args.interval)),
            )
            if args.cycles and cycles >= args.cycles:
                return 0
            time.sleep(args.interval)
