import importlib
import sys
import types
from types import SimpleNamespace


class FakeDB:
    def __init__(self, outstanding_amount=None):
        self.outstanding_amount = outstanding_amount

    def get_value(self, doctype, name, fieldname):
        assert doctype == "Sales Invoice"
        assert name == "SINV-0001"
        assert fieldname == "outstanding_amount"
        return self.outstanding_amount


class FakeFrappe(types.ModuleType):
    def __init__(self, outstanding_amount=None):
        super().__init__("frappe")
        self.db = FakeDB(outstanding_amount)
        self._ = lambda value: value

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator


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


def test_reference_allocation_snaps_one_cent_residual(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request(), 546.05)

    assert amount == 546.06


def test_reference_allocation_does_not_hide_larger_underpayment(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request(), 545.00)

    assert amount == 545.00


def test_reference_allocation_ignores_sales_order_deposits(monkeypatch):
    webhook = load_webhook(monkeypatch, outstanding_amount=546.06)

    amount = webhook.get_reference_allocation_amount(payment_request("Sales Order"), 100.00)

    assert amount == 100.00


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
