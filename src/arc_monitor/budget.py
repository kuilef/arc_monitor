"""Pacing, per-run reservations and observed account quotas; estimates are not bills."""

from __future__ import annotations

import math
import time


class BudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(
        self,
        *,
        max_requests=100,
        rpm=6,
        points_budget=100,
        points_per_request=10,
        reserve_points=20,
        clock=time.monotonic,
        sleep=time.sleep,
        on_change=None,
    ):
        values = [max_requests, rpm, points_budget, points_per_request]
        if (
            any(not math.isfinite(x) or x <= 0 for x in values)
            or not math.isfinite(reserve_points)
            or reserve_points < 0
        ):
            raise ValueError("Request and point limits must be finite and positive")
        self.max_requests = max_requests
        self.rpm = rpm
        self.points_budget = points_budget
        self.points_per_request = points_per_request
        self.reserve_points = reserve_points
        self.clock, self.sleep = clock, sleep
        self.on_change = on_change
        self.requests = 0
        self.reserved_points = 0.0
        self.account_remaining = None
        self.baseline_used = None
        self.observed_used = None
        self.last_request = None
        self.last_usage = None

    def observe_usage(self, body):
        try:
            if body["status"] != "active":
                raise BudgetExceeded("Bitquery billing period is not active")
            period = body["billing_period"]
            total = float(period["limits"]["points_limit"])
            used = float(period["usage"]["points_usage"])
            rpm = float(period["limits"]["rate_limit"])
            if not all(math.isfinite(v) for v in (total, used, rpm)) or total <= 0 or used < 0 or rpm <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise BudgetExceeded("Bitquery usage response incomplete; collection stopped") from None
        self.rpm = min(self.rpm, rpm)
        self.baseline_used = used if self.baseline_used is None else self.baseline_used
        self.observed_used = used
        # Do not release reservations on unchanged/delayed counters. Conservative double
        # reservation after an update is intentional: delayed account billing is ambiguous.
        self.account_remaining = total - used - self.reserved_points
        self.last_usage = body
        self._check(0)

    def _check(self, charge):
        if self.requests >= self.max_requests:
            raise BudgetExceeded("HTTP request budget exhausted")
        observed = max(0, (self.observed_used or 0) - (self.baseline_used or 0))
        if max(observed, self.reserved_points) + charge > self.points_budget:
            raise BudgetExceeded("Local point reservation budget exhausted")
        if self.account_remaining is not None and self.account_remaining - charge < self.reserve_points:
            raise BudgetExceeded("Bitquery account point reserve reached")

    def consume(self, *, charge=True):
        points = self.points_per_request if charge else 0
        self._check(points)
        if self.last_request is not None:
            delay = 60 / self.rpm - (self.clock() - self.last_request)
            if delay > 0:
                self.sleep(delay)
        self._check(points)
        previous_request = self.last_request
        self.last_request = self.clock()
        self.requests += 1
        self.reserved_points += points
        if self.account_remaining is not None:
            self.account_remaining -= points
        if self.on_change:
            try:
                self.on_change(self.summary())
            except BudgetExceeded:
                # A persistent preflight limit refused the reservation; no HTTP
                # attempt happened and counters must not report one.
                self.last_request = previous_request
                self.requests -= 1
                self.reserved_points -= points
                if self.account_remaining is not None:
                    self.account_remaining += points
                raise

    def summary(self):
        return {
            "http_requests": self.requests,
            "reserved_points": self.reserved_points,
            "observed_points_used": self.observed_used,
            "account_remaining_conservative": self.account_remaining,
            "points_budget": self.points_budget,
            "points_per_request_reserve": self.points_per_request,
            "requests_per_minute": self.rpm,
            "cost_note": "Local estimates protect against delayed usage; not an exact supplier charge cap.",
        }
