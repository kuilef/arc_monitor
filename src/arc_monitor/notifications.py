"""Explicit Telegram delivery after a bound, eligible temporal evaluation only."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx

from .models import iso, parse_time


class DeliveryUncertain(RuntimeError):
    pass


class TelegramSender:
    def __init__(self, token, chat_id, *, client=None):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token or "") or not str(chat_id).strip():
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID locally")
        self._token, self._chat = token, str(chat_id)
        self.client = client or httpx.Client(timeout=20)

    def send(self, text):
        try:
            response = self.client.post(
                "https://api.telegram.org/bot" + self._token + "/sendMessage",
                json={
                    "chat_id": self._chat,
                    "text": text[:4000],
                    "link_preview_options": {"is_disabled": True},
                },
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise DeliveryUncertain("Telegram delivery outcome unknown; no automatic resend") from None
        if response.status_code >= 500:
            raise DeliveryUncertain("Telegram server failure; delivery outcome unknown")
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError(f"Telegram rejected message (HTTP {response.status_code})")
        try:
            return int(body["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            raise DeliveryUncertain("Telegram acknowledgment missing; no automatic resend") from None

    def close(self):
        self.client.close()


def notification_gate(store, at):
    from .evaluation import dataset_fingerprint, verify_report_fingerprints

    saved = store.get_state("evaluation")
    if not saved:
        return None, "no_evaluation"
    try:
        blob = Path(saved["path"]).read_bytes()
        if hashlib.sha256(blob).hexdigest() != saved["sha256"]:
            return None, "evaluation_changed"
        report = json.loads(blob)
        if not verify_report_fingerprints(report):
            return None, "invalid_evaluation_fingerprint"
        observed = parse_time(report["as_of"])
        if store.mode != "live" or report["mode"] != "live":
            return None, "synthetic_data"
        if not timedelta(0) <= at - observed <= timedelta(days=7):
            return None, "evaluation_expired_or_future"
        if report.get("notification_eligible") is not True:
            return None, "research_not_eligible"
        if min(report["sample_counts"]["train"], report["sample_counts"]["test"]) < 10:
            return None, "insufficient_independent_samples"
        if report["fingerprints"]["data_sha256"] != dataset_fingerprint(store, observed):
            return None, "historical_dataset_changed"
        return report, "eligible"
    except (OSError, ValueError, KeyError, TypeError):
        return None, "invalid_evaluation"


def notify(store, at, *, send=False, sender=None, max_messages=10):
    if not 1 <= max_messages <= 100:
        raise ValueError("Notification batch must be 1..100")
    report, gate = notification_gate(store, at)
    result = {"gate": gate, "sent": 0, "prepared": 0, "skipped": 0, "expired": 0, "send_requested": send}
    if not report:
        return result
    from .evaluation import matches_rule
    from .metrics import origins, snapshot

    config = report["configuration"]
    rule = report["selection"]["rule"]
    owned = False
    try:
        for launch in origins(store, at).values():
            cutoff = launch.ts + timedelta(minutes=config["feature_minutes"])
            due = cutoff + timedelta(seconds=config["latency_seconds"])
            # Apply the same feature time and latency tested by the research protocol.
            if launch.origin != "launch_event" or at < due:
                continue
            if not store.covered("trades", launch.ts, cutoff):
                continue
            features = snapshot(store, launch.token, cutoff)
            if not matches_rule(rule, features) or features["risk_score"] is None:
                continue
            key = hashlib.sha256(
                (report["fingerprints"]["gate_sha256"] + launch.token + iso(cutoff)).encode()
            ).hexdigest()
            existing = store.get_state("notification:" + key)
            if existing and existing["status"] in {"sent", "sending", "uncertain", "expired"}:
                result["skipped"] += 1
                continue
            if at >= due + timedelta(seconds=60):
                # Late delivery would no longer match the evaluated entry latency.
                # Record it explicitly, rather than silently losing the candidate.
                result["expired"] += 1
                if send:
                    store.set_state(
                        "notification:" + key,
                        {
                            "token": launch.token,
                            "status": "expired",
                            "at": iso(at),
                            "feature_cutoff": iso(cutoff),
                            "reason": "delivery_window_missed",
                        },
                    )
                continue
            result["prepared"] += 1
            if not send:
                continue
            if sender is None:
                sender = TelegramSender(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
                owned = True
            state = {"token": launch.token, "feature_cutoff": iso(cutoff), "at": iso(at), "status": "sending"}
            store.set_state("notification:" + key, state)
            try:
                message_id = sender.send(
                    "Arc research candidate\n"
                    + launch.token
                    + "\nRule: "
                    + rule
                    + "\nFeature cutoff: "
                    + iso(cutoff)
                    + "\nResearch observation; not a trading instruction."
                )
            except DeliveryUncertain:
                store.set_state("notification:" + key, {**state, "status": "uncertain"})
                raise
            except RuntimeError:
                store.set_state("notification:" + key, {**state, "status": "rejected"})
                raise
            store.set_state("notification:" + key, {**state, "status": "sent", "message_id": message_id})
            result["sent"] += 1
            if result["sent"] >= max_messages:
                break
            time.sleep(1)
    finally:
        if owned:
            sender.close()
    return result
