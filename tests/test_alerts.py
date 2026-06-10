"""M5 alert-service tests."""
from services.alerts import AlertService, LogSink, telegram_payload


class GoodSink:
    def __init__(self):
        self.got = []

    def send(self, level, message):
        self.got.append((level, message))


class BadSink:
    def send(self, level, message):
        raise RuntimeError("boom")


def test_fan_out_to_all_sinks():
    a, b = GoodSink(), GoodSink()
    AlertService([a, b]).alert("error", "x")
    assert a.got == [("error", "x")]
    assert b.got == [("error", "x")]


def test_min_level_filter():
    sink = GoodSink()
    svc = AlertService([sink], min_level="error")
    svc.alert("info", "ignored")
    svc.alert("critical", "kept")
    assert sink.got == [("critical", "kept")]


def test_failing_sink_does_not_break_others():
    good = GoodSink()
    AlertService([BadSink(), good]).alert("warning", "x")
    assert good.got == [("warning", "x")]


def test_log_sink_records():
    sink = LogSink()
    sink.send("info", "hello")
    assert sink.records == [("info", "hello")]


def test_telegram_payload():
    url, data = telegram_payload("TOK", "42", "critical", "flatten failed")
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert data == {"chat_id": "42", "text": "[CRITICAL] flatten failed"}
