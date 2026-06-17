import importlib
import sys
import types
from types import SimpleNamespace


class FakeDB:
    def __init__(self):
        self.queries = []
        self.values = []
        self.payment_requests = [
            SimpleNamespace(
                name="PAY-REQ-0001",
                status="Requested",
                stripe_invoice_id=None,
                stripe_payment_status="N/A",
            )
        ]

    def sql(self, query, as_dict=False):
        self.queries.append((query, as_dict))
        assert as_dict is True
        return self.payment_requests

    def set_value(self, doctype, name, values, update_modified=False):
        self.values.append((doctype, name, values, update_modified))


class FakeFrappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self.db = FakeDB()
        self._ = lambda value: value

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator


def load_patch(monkeypatch):
    fake_frappe = FakeFrappe()
    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
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
    sys.modules.pop("payments.patches.v1_0.sync_manually_paid_payment_requests", None)
    patch = importlib.import_module("payments.patches.v1_0.sync_manually_paid_payment_requests")
    return patch, fake_frappe


def test_patch_marks_manual_na_payment_request_paid(monkeypatch):
    patch, fake_frappe = load_patch(monkeypatch)

    patch.execute()

    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-0001",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]
