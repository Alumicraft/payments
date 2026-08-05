import importlib
import sys
import types
from types import SimpleNamespace

import pytest


class FakeDB:
    def __init__(self, outstanding_amount=None):
        self.outstanding_amount = outstanding_amount
        self.payment_request_name = None
        self.existing_payment_entry = None
        self.set_values = []

    def get_value(self, doctype, name_or_filters, fieldname):
        if doctype == "Sales Invoice":
            assert name_or_filters == "SINV-0001"
            assert fieldname == "outstanding_amount"
            return self.outstanding_amount

        if doctype == "Payment Request":
            assert name_or_filters == {"stripe_invoice_id": "in_123"}
            assert fieldname == "name"
            return self.payment_request_name

        raise AssertionError(f"Unexpected get_value: {doctype}, {name_or_filters}, {fieldname}")

    def exists(self, doctype, filters):
        if doctype == "Payment Entry":
            assert filters == {
                "reference_no": "pi_123",
                "docstatus": 1,
            }
            return self.existing_payment_entry

        raise AssertionError(f"Unexpected exists: {doctype}, {filters}")

    def set_value(self, doctype, name, values, update_modified=False):
        self.set_values.append((doctype, name, values, update_modified))

    def commit(self):
        pass


class FakeFrappe(types.ModuleType):
    def __init__(self, outstanding_amount=None):
        super().__init__("frappe")
        self.db = FakeDB(outstanding_amount)
        self._ = lambda value: value

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_doc(self, doctype, name):
        assert doctype == "Payment Request"
        assert name == "PAY-REQ-0001"
        return SimpleNamespace(
            name=name,
            stripe_payment_status="Paid",
            status="Paid",
            allow_card_payment=False,
            card_processing_fee=0,
        )

    def get_single(self, doctype):
        assert doctype == "Stripe Settings"
        return SimpleNamespace(get_password=lambda fieldname: "sk_test")

    def log_error(self, message, title=None):
        pass


def load_webhook(monkeypatch, outstanding_amount=None):
    fake_frappe = FakeFrappe(outstanding_amount)

    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.utils",
        SimpleNamespace(
            flt=lambda value, precision=2: round(float(value or 0), precision),
            now_datetime=lambda: None,
        ),
    )

    sys.modules.pop("payments.webhook", None)
    return importlib.import_module("payments.webhook")


def payment_request(reference_doctype="Sales Invoice"):
    return SimpleNamespace(
        reference_doctype=reference_doctype,
        reference_name="SINV-0001",
    )


class StripeLikeObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        raise AttributeError(name)


def test_reference_allocation_snaps_one_cent_residual(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request(), 546.05)

    assert amount == 546.06


def test_reference_allocation_snaps_live_card_fee_cent_residual(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=40978.23)

    amount = webhook.get_reference_allocation_amount(payment_request(), 40978.22)

    assert amount == 40978.23


def test_reference_allocation_does_not_hide_larger_underpayment(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request(), 545.00)

    assert amount == 545.00


def test_reference_allocation_ignores_sales_order_deposits(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request("Sales Order"), 100.00)

    assert amount == 100.00


def test_customer_payment_amount_subtracts_recorded_card_fee_without_allow_flag(monkeypatch):
    webhook = load_webhook(monkeypatch)

    amount = webhook.get_customer_payment_amount(
        SimpleNamespace(allow_card_payment=False, card_processing_fee=1229.35),
        42207.57,
    )

    assert amount == 40978.22


def test_payment_intent_invoice_id_uses_order_reference(monkeypatch):
    webhook = load_webhook(monkeypatch)

    invoice_id = webhook.get_payment_intent_invoice_id(
        {
            "id": "pi_123",
            "payment_details": {
                "order_reference": "in_123",
            },
        }
    )

    assert invoice_id == "in_123"


def test_payment_intent_invoice_id_handles_stripe_object_values(monkeypatch):
    webhook = load_webhook(monkeypatch)

    invoice_id = webhook.get_payment_intent_invoice_id(
        StripeLikeObject(
            id="pi_123",
            invoice=None,
            payment_details=StripeLikeObject(order_reference="in_123"),
            metadata=StripeLikeObject(),
        )
    )

    assert invoice_id == "in_123"


def test_payment_intent_invoice_id_prefers_invoice_field(monkeypatch):
    webhook = load_webhook(monkeypatch)

    invoice_id = webhook.get_payment_intent_invoice_id(
        {
            "id": "pi_123",
            "invoice": "in_direct",
            "payment_details": {
                "order_reference": "in_order_reference",
            },
        }
    )

    assert invoice_id == "in_direct"


def test_payment_intent_succeeded_creates_missing_entry_for_paid_request(monkeypatch):
    webhook = load_webhook(monkeypatch)
    webhook.frappe.db.payment_request_name = "PAY-REQ-0001"
    created = []

    class FakeInvoice:
        @staticmethod
        def retrieve(invoice_id):
            assert invoice_id == "in_123"
            return {
                "id": invoice_id,
                "payment_intent": "pi_123",
                "amount_paid": 10000,
                "currency": "usd",
            }

    monkeypatch.setitem(
        sys.modules,
        "stripe",
        SimpleNamespace(Invoice=FakeInvoice),
    )
    monkeypatch.setattr(
        webhook,
        "create_payment_entry",
        lambda payment_request, invoice, stripe_fee=0: created.append(
            (payment_request.name, invoice["id"], stripe_fee)
        )
        or SimpleNamespace(name="ACC-PAY-0001"),
    )

    result = webhook.handle_payment_intent_succeeded(
        {
            "data": {
                "object": {
                    "id": "pi_123",
                    "invoice": "in_123",
                }
            }
        }
    )

    assert created == [("PAY-REQ-0001", "in_123", 0)]
    assert result["payment_entry"] == "ACC-PAY-0001"


def test_payment_intent_failure_propagates_for_stripe_retry(monkeypatch):
    webhook = load_webhook(monkeypatch)
    webhook.frappe.db.payment_request_name = "PAY-REQ-0001"

    class FakeInvoice:
        @staticmethod
        def retrieve(invoice_id):
            return {
                "id": invoice_id,
                "payment_intent": "pi_123",
                "amount_paid": 10000,
                "currency": "usd",
            }

    monkeypatch.setitem(sys.modules, "stripe", SimpleNamespace(Invoice=FakeInvoice))
    monkeypatch.setattr(
        webhook,
        "create_payment_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("accounting unavailable")),
    )

    with pytest.raises(RuntimeError, match="accounting unavailable"):
        webhook.handle_payment_intent_succeeded(
            {
                "data": {
                    "object": {
                        "id": "pi_123",
                        "invoice": "in_123",
                    }
                }
            }
        )
