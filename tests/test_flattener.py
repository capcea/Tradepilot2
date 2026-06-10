"""M5 flattener tests (REQUIRED): closes positions and verifies flat against a
fake broker; escalation path when the broker will not close."""
from decimal import Decimal

from ports.execution import OrderResult
from services.flattener import flatten_all
from tests.fakes import FakeBroker, RecordingAlerts

D = Decimal


def test_already_flat_is_ok():
    broker, alerts = FakeBroker(), RecordingAlerts()
    report = flatten_all(broker, alerts, sleep=lambda s: None)
    assert report.ok
    assert report.closed_tickets == ()
    assert report.rounds == 1


def test_closes_all_positions_and_verifies_flat():
    broker, alerts = FakeBroker(), RecordingAlerts()
    broker.add_position(ticket="P1")
    broker.add_position(ticket="P2", symbol="GBPUSD", side="short")
    report = flatten_all(broker, alerts, sleep=lambda s: None)
    assert report.ok
    assert set(report.closed_tickets) == {"P1", "P2"}
    assert broker.positions() == []
    assert not report.escalated


def test_retries_when_close_fails_once():
    broker, alerts = FakeBroker(), RecordingAlerts()
    broker.add_position(ticket="P1")
    broker.queue_close(OrderResult(ok=False, error="busy", retryable=True))
    report = flatten_all(broker, alerts, sleep=lambda s: None)
    assert report.ok
    assert report.rounds == 2
    assert broker.positions() == []


def test_escalates_when_position_will_not_close():
    broker, alerts = RecordingAlertsBroker(), RecordingAlerts()
    report = flatten_all(broker, alerts, max_rounds=3, sleep=lambda s: None)
    assert not report.ok
    assert report.escalated
    assert report.remaining == ("STUCK",)
    assert any(level == "critical" for level, _ in alerts.messages)


class RecordingAlertsBroker(FakeBroker):
    """Broker whose position never closes."""

    def __init__(self):
        super().__init__()
        self.add_position(ticket="STUCK")

    def close_position(self, ticket, lots=None):
        self.close_calls.append((ticket, lots))
        return OrderResult(ok=False, error="rejected", retryable=True)
