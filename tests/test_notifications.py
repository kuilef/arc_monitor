from datetime import datetime, timezone
import httpx
import pytest


def test_telegram_transport_failure_is_uncertain_and_not_retried():
    from arc_monitor.notifications import TelegramSender, DeliveryUncertain

    attempts = []

    def fail(req):
        attempts.append(req)
        raise httpx.ReadTimeout("secret token in unsafe provider text")

    sender = TelegramSender("123:secret", "12345", client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(DeliveryUncertain) as e:
        sender.send("hello")
    assert "secret" not in str(e.value) and len(attempts) == 1


def test_insufficient_evaluation_cannot_send(tmp_path):
    from arc_monitor.notifications import notify
    from arc_monitor.storage import Store

    with Store(":memory:") as s:
        result = notify(s, datetime.now(timezone.utc), send=True)
        assert result["sent"] == 0 and result["gate"] == "no_evaluation"


def test_uncertain_delivery_is_persisted_and_not_retried(monkeypatch):
    from datetime import timedelta
    from arc_monitor import notifications, metrics, evaluation
    from arc_monitor.models import Launch
    from arc_monitor.storage import Store

    at = datetime(2026, 9, 22, tzinfo=timezone.utc)
    launch = Launch(id="launch", token="0x" + "1" * 40, ts=at, source="test", tx="tx", origin="launch_event")
    report = {
        "configuration": {"feature_minutes": 15, "latency_seconds": 120},
        "selection": {"rule": "runner_65"},
        "fingerprints": {"gate_sha256": "gate"},
    }
    monkeypatch.setattr(notifications, "notification_gate", lambda s, t: (report, "eligible"))
    monkeypatch.setattr(metrics, "origins", lambda s, t: {launch.token: launch})
    monkeypatch.setattr(metrics, "snapshot", lambda *a: {"risk_score": 90})
    monkeypatch.setattr(evaluation, "matches_rule", lambda *a: True)

    class Sender:
        count = 0

        def send(self, text):
            self.count += 1
            raise notifications.DeliveryUncertain("unknown")

    sender = Sender()
    with Store(":memory:") as s:
        s.ingest([launch])
        s.record_window("trades", at, at + timedelta(minutes=18), [], "test")
        with pytest.raises(notifications.DeliveryUncertain):
            notifications.notify(s, at + timedelta(minutes=17), send=True, sender=sender)
        result = notifications.notify(s, at + timedelta(minutes=17), send=True, sender=sender)
        assert result["sent"] == 0 and result["skipped"] == 1 and sender.count == 1


def test_saved_report_change_closes_gate(tmp_path):
    import hashlib
    from arc_monitor.notifications import notification_gate
    from arc_monitor.storage import Store

    report = tmp_path / "evaluation.json"
    report.write_text("{}")
    with Store(":memory:") as s:
        s.set_state("evaluation", {"path": str(report), "sha256": hashlib.sha256(b"{}").hexdigest()})
        report.write_text('{"notification_eligible":true}')
        assert notification_gate(s, datetime.now(timezone.utc))[1] == "evaluation_changed"
