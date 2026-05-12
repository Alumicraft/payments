import importlib
import sys
import types
from types import SimpleNamespace


class FakeStripeError(Exception):
    pass


class FakeDB:
    def __init__(self):
        self.values = []
        self.sql_queries = []

    def set_value(self, doctype, name, values, update_modified=False):
        self.values.append((doctype, name, values, update_modified))

    def sql(self, query):
        self.sql_queries.append(query)


class FakeFrappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self.db = FakeDB()
        self._ = lambda value: value
        self.payment_requests = []
        self.messages = []
        self.errors = []

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_all(self, doctype, filters=None, fields=None):
        assert doctype == "Payment Request"
        return self.payment_requests

    def msgprint(self, message, **kwargs):
        self.messages.append((message, kwargs))

    def log_error(self, message, title=None):
        self.errors.append((message, title))


class FakeStripe(types.ModuleType):
    def __init__(self, invoice_status):
        super().__init__("stripe")
        self.api_key = None
        self.error = SimpleNamespace(StripeError=FakeStripeError)
        self.Invoice = SimpleNamespace(
            retrieve=self.retrieve,
            delete=self.delete,
            void_invoice=self.void_invoice,
        )
        self.invoice_status = invoice_status
        self.deleted = []
        self.voided = []

    def retrieve(self, invoice_id):
        return SimpleNamespace(id=invoice_id, status=self.invoice_status)

    def delete(self, invoice_id):
        self.deleted.append(invoice_id)

    def void_invoice(self, invoice_id):
        self.voided.append(invoice_id)


def load_utils(monkeypatch, invoice_status="paid", settings=None):
    fake_frappe = FakeFrappe()
    fake_stripe = FakeStripe(invoice_status)

    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.utils",
        SimpleNamespace(
            now_datetime=lambda: None,
            get_datetime=lambda value: value,
            time_diff_in_seconds=lambda current, previous: 0,
        ),
    )

    sys.modules.pop("payments.utils", None)
    utils = importlib.import_module("payments.utils")
    monkeypatch.setattr(utils, "get_stripe_settings", lambda: settings)
    return utils, fake_frappe, fake_stripe


def payment_entry():
    return SimpleNamespace(
        name="ACC-PAY-2026-00001",
        references=[
            SimpleNamespace(
                reference_doctype="Sales Invoice",
                reference_name="SINV-0001",
            )
        ],
    )


def test_paid_payment_request_update_syncs_pending_stripe_status(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    doc = SimpleNamespace(
        docstatus=1,
        status="Paid",
        outstanding_amount=0,
        stripe_invoice_id="in_paid",
        stripe_payment_status="Pending",
        db_set=lambda *args, **kwargs: fake_frappe.db.set_value("Payment Request", "PAY-REQ-PAID", args, kwargs),
    )

    utils.sync_paid_payment_request_status(doc)

    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-PAID",
            ("stripe_payment_status", "Paid"),
            {"update_modified": False},
        )
    ]


def test_paid_payment_request_update_voids_open_stripe_invoice(monkeypatch):
    settings = SimpleNamespace(get_password=lambda fieldname: "sk_test")
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "open", settings=settings)
    doc = SimpleNamespace(
        docstatus=1,
        status="Paid",
        outstanding_amount=0,
        stripe_invoice_id="in_open",
        stripe_payment_status="Pending",
        db_set=lambda *args, **kwargs: fake_frappe.db.set_value("Payment Request", "PAY-REQ-PAID", args, kwargs),
    )

    utils.sync_paid_payment_request_status(doc)

    assert fake_stripe.voided == ["in_open"]
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-PAID",
            ("stripe_payment_status", "Voided"),
            {"update_modified": False},
        )
    ]


def test_submitted_payment_entry_marks_request_paid_when_stripe_invoice_already_paid(monkeypatch):
    settings = SimpleNamespace(get_password=lambda fieldname: "sk_test")
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings)
    fake_frappe.payment_requests = [
        SimpleNamespace(name="PAY-REQ-0001", stripe_invoice_id="in_123")
    ]

    utils.void_stripe_invoice_on_manual_payment(payment_entry())

    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-0001",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]
    assert fake_stripe.deleted == []
    assert fake_stripe.voided == []


def test_submitted_payment_entry_marks_request_paid_after_voiding_open_invoice(monkeypatch):
    settings = SimpleNamespace(get_password=lambda fieldname: "sk_test")
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "open", settings)
    fake_frappe.payment_requests = [
        SimpleNamespace(name="PAY-REQ-0002", stripe_invoice_id="in_456")
    ]

    utils.void_stripe_invoice_on_manual_payment(payment_entry())

    assert fake_stripe.voided == ["in_456"]
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-0002",
            {"status": "Paid", "stripe_payment_status": "Voided"},
            False,
        )
    ]


def test_submitted_payment_entry_marks_request_paid_without_stripe_settings(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "open", settings=None)
    fake_frappe.payment_requests = [
        SimpleNamespace(name="PAY-REQ-0003", stripe_invoice_id="in_789")
    ]

    utils.void_stripe_invoice_on_manual_payment(payment_entry())

    assert fake_stripe.voided == []
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-0003",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]
