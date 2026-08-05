import importlib
import sys
import types
from types import SimpleNamespace

import pytest


class AuthenticationError(Exception):
    pass


class SignatureVerificationError(Exception):
    pass


class FakeFrappe(types.ModuleType):
    def __init__(self, webhook_secret, signature="valid-signature", settings_error=None):
        super().__init__("frappe")
        self._ = lambda value: value
        self.AuthenticationError = AuthenticationError
        self.ValidationError = ValueError
        self.request = SimpleNamespace(
            get_data=lambda as_text=False: '{"id":"evt_123","type":"invoice.paid"}',
            headers={"Stripe-Signature": signature} if signature else {},
        )
        self.webhook_secret = webhook_secret
        self.settings_error = settings_error
        self.errors = []
        self.users = []
        self.webhook_status = None
        self.commits = 0
        self.db = SimpleNamespace(get_value=self.get_db_value, commit=self.commit)

    def commit(self):
        self.commits += 1

    def get_db_value(self, doctype, filters, fieldname):
        assert doctype == "Stripe Webhook Event"
        assert filters == {"event_id": "evt_123"}
        assert fieldname == "status"
        return self.webhook_status

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_single(self, doctype):
        assert doctype == "Stripe Settings"
        if self.settings_error:
            raise self.settings_error
        return SimpleNamespace(
            get_password=lambda fieldname: (
                self.webhook_secret if fieldname == "webhook_secret" else "sk_test"
            )
        )

    def log_error(self, message, title=None):
        self.errors.append((message, title))

    def throw(self, message, exception):
        raise exception(message)

    def set_user(self, user):
        self.users.append(user)


class FakeStripe(types.ModuleType):
    def __init__(self, event=None, error=None):
        super().__init__("stripe")
        self.api_key = None
        self.error = SimpleNamespace(SignatureVerificationError=SignatureVerificationError)
        self.event = event or {"id": "evt_123", "type": "invoice.paid"}
        self.error_to_raise = error
        self.calls = []
        self.Webhook = SimpleNamespace(construct_event=self.construct_event)

    def construct_event(self, payload, signature, secret):
        self.calls.append((payload, signature, secret))
        if self.error_to_raise:
            raise self.error_to_raise
        return self.event


def load_webhook(
    monkeypatch,
    webhook_secret,
    signature="valid-signature",
    stripe_error=None,
    settings_error=None,
):
    fake_frappe = FakeFrappe(webhook_secret, signature, settings_error)
    fake_stripe = FakeStripe(error=stripe_error)
    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.utils",
        SimpleNamespace(flt=lambda value, precision=2: round(float(value or 0), precision), now_datetime=lambda: None),
    )
    sys.modules.pop("payments.webhook", None)
    return importlib.import_module("payments.webhook"), fake_frappe, fake_stripe


def test_webhook_rejects_requests_when_secret_is_not_configured(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(monkeypatch, webhook_secret=None)

    with pytest.raises(AuthenticationError, match="not configured"):
        webhook.handle_stripe_webhook()

    assert fake_stripe.calls == []
    assert fake_frappe.users == []


def test_webhook_configuration_error_propagates_for_stripe_retry(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(
        monkeypatch,
        webhook_secret=None,
        settings_error=RuntimeError("settings unavailable"),
    )

    with pytest.raises(RuntimeError, match="settings unavailable"):
        webhook.handle_stripe_webhook()

    assert fake_stripe.calls == []
    assert fake_frappe.users == []


def test_webhook_rejects_missing_signature(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(
        monkeypatch,
        webhook_secret="whsec_123",
        signature=None,
    )

    with pytest.raises(AuthenticationError, match="Missing"):
        webhook.handle_stripe_webhook()

    assert fake_stripe.calls == []
    assert fake_frappe.users == []


def test_webhook_rejects_invalid_signature(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(
        monkeypatch,
        webhook_secret="whsec_123",
        stripe_error=SignatureVerificationError("bad signature"),
    )

    with pytest.raises(AuthenticationError, match="Invalid"):
        webhook.handle_stripe_webhook()

    assert len(fake_stripe.calls) == 1
    assert fake_frappe.users == []


def test_webhook_elevates_only_after_valid_signature(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(
        monkeypatch,
        webhook_secret="whsec_123",
    )
    monkeypatch.setattr(webhook, "is_event_processed", lambda event_id: True)

    result = webhook.handle_stripe_webhook()

    assert result == {"status": "already_processed", "event_id": "evt_123"}
    assert len(fake_stripe.calls) == 1
    assert fake_frappe.users == ["Administrator"]


@pytest.mark.parametrize("status", ["Processing", "Success"])
def test_webhook_skips_only_processing_or_successful_events(monkeypatch, status):
    webhook, fake_frappe, fake_stripe = load_webhook(monkeypatch, webhook_secret="whsec_123")
    fake_frappe.webhook_status = status

    assert webhook.is_event_processed("evt_123") is True


@pytest.mark.parametrize("status", [None, "Failed"])
def test_webhook_retries_missing_or_failed_events(monkeypatch, status):
    webhook, fake_frappe, fake_stripe = load_webhook(monkeypatch, webhook_secret="whsec_123")
    fake_frappe.webhook_status = status

    assert webhook.is_event_processed("evt_123") is False


def test_webhook_processing_failure_returns_error_to_stripe(monkeypatch):
    webhook, fake_frappe, fake_stripe = load_webhook(monkeypatch, webhook_secret="whsec_123")
    event_doc = SimpleNamespace(status="Processing", error_message=None, save=lambda **kwargs: None)
    monkeypatch.setattr(webhook, "is_event_processed", lambda event_id: False)
    monkeypatch.setattr(webhook, "record_webhook_event", lambda event: event_doc)
    monkeypatch.setattr(
        webhook,
        "process_event",
        lambda event, event_type: (_ for _ in ()).throw(RuntimeError("posting failed")),
    )

    with pytest.raises(RuntimeError, match="posting failed"):
        webhook.handle_stripe_webhook()

    assert event_doc.status == "Failed"
    assert event_doc.error_message == "posting failed"
    assert fake_frappe.commits == 1
