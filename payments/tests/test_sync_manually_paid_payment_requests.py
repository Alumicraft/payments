import importlib
import sys
import types
from types import SimpleNamespace


class FakeDB:
    def __init__(self):
        self.queries = []
        self.values = []
        self.exact_payment_requests = [
            SimpleNamespace(
                name="PAY-REQ-0001",
                status="Requested",
                stripe_invoice_id=None,
                stripe_payment_status="N/A",
            )
        ]
        self.project_payment_requests = []
        self.project_payment_entries = []

    def sql(self, query, values=None, as_dict=False):
        self.queries.append((query, values, as_dict))
        assert as_dict is True
        if "from `tabPayment Entry` pe" in query:
            return self.project_payment_entries
        if "coalesce(nullif(pr.project" in query:
            return self.project_payment_requests
        return self.exact_payment_requests

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


def test_patch_marks_sales_order_request_paid_from_project_invoice_payment(monkeypatch):
    patch, fake_frappe = load_patch(monkeypatch)
    fake_frappe.db.exact_payment_requests = []
    fake_frappe.db.project_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-SO",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
            grand_total=1595,
            project="GAR100326",
        )
    ]
    fake_frappe.db.project_payment_entries = [SimpleNamespace(name="PAY-ENTRY-SINV")]

    patch.execute()

    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-SO",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]


def test_patch_skips_sales_order_request_without_matching_project_payment(monkeypatch):
    patch, fake_frappe = load_patch(monkeypatch)
    fake_frappe.db.exact_payment_requests = []
    fake_frappe.db.project_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-UNPAID",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="N/A",
            grand_total=533.75,
            project="GRA100626",
        )
    ]
    fake_frappe.db.project_payment_entries = []

    patch.execute()

    assert fake_frappe.db.values == []
