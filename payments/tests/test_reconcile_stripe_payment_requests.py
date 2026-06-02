import importlib
import sys
import types
from types import SimpleNamespace


class FakeDB:
    def __init__(self):
        self.set_values = []

    def get_value(self, doctype, name, fieldname):
        assert doctype == "Sales Invoice"
        assert name == "SINV-0001"
        assert fieldname == "outstanding_amount"
        return 42207.57

    def exists(self, doctype, filters):
        assert doctype == "Payment Entry"
        assert filters == {
            "reference_no": ["in", ["in_1TbopE2LDx66zyddwQ9RyLdx"]],
            "docstatus": ["!=", 2],
        }
        return False

    def set_value(self, doctype, name, fieldname, value, update_modified=False):
        self.set_values.append((doctype, name, fieldname, value, update_modified))


class FakeFrappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self.db = FakeDB()
        self._ = lambda value: value
        self.errors = []

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_all(self, doctype, filters=None, fields=None):
        assert doctype == "Payment Request"
        return [
            SimpleNamespace(
                name="PAY-REQ-0001",
                stripe_invoice_id="in_1TbopE2LDx66zyddwQ9RyLdx",
                stripe_payment_intent_id=None,
                reference_name="SINV-0001",
                grand_total=42207.57,
            )
        ]

    def get_doc(self, doctype, name):
        assert doctype == "Payment Request"
        assert name == "PAY-REQ-0001"
        return SimpleNamespace(name=name)

    def log_error(self, message, title=None):
        self.errors.append((message, title))


class FakeStripe(types.ModuleType):
    def __init__(self):
        super().__init__("stripe")
        self.api_key = None
        self.Invoice = SimpleNamespace(retrieve=self.retrieve_invoice)
        self.PaymentIntent = SimpleNamespace(list=self.list_payment_intents)

    def retrieve_invoice(self, invoice_id):
        assert invoice_id == "in_1TbopE2LDx66zyddwQ9RyLdx"
        return SimpleNamespace(
            status="paid",
            to_dict_recursive=lambda: {
                "id": invoice_id,
                "status": "paid",
                "amount_paid": 4220757,
                "currency": "usd",
                "customer": "cus_U8x9Yg2kTidTUs",
                "created": 1767225600,
                "payment_intent": None,
                "charge": None,
            },
        )

    def list_payment_intents(self, customer, created=None, limit=None):
        assert customer == "cus_U8x9Yg2kTidTUs"
        assert created == {"gte": 1767225600}
        assert limit == 100
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="pi_3TbopG2LDx66zydd1GOgN5r7",
                    status="succeeded",
                    amount=4220757,
                    currency="usd",
                    invoice=None,
                    payment_details=None,
                    metadata={},
                )
            ]
        )


def load_reconcile(monkeypatch):
    fake_frappe = FakeFrappe()
    fake_stripe = FakeStripe()

    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.utils",
        SimpleNamespace(
            flt=lambda value, precision=2: round(float(value or 0), precision),
            now_datetime=lambda: None,
            get_datetime=lambda value: value,
            time_diff_in_seconds=lambda current, previous: 0,
        ),
    )

    sys.modules.pop("payments.utils", None)
    sys.modules.pop("payments.webhook", None)
    sys.modules.pop("payments.patches.v1_0.reconcile_stripe_payment_requests", None)
    reconcile = importlib.import_module("payments.patches.v1_0.reconcile_stripe_payment_requests")
    return reconcile, fake_frappe


def test_reconciliation_uses_matching_payment_intent_when_invoice_missing_reference(monkeypatch):
    reconcile, fake_frappe = load_reconcile(monkeypatch)
    created = []

    monkeypatch.setattr(
        reconcile,
        "create_payment_entry",
        lambda payment_request, stripe_invoice: created.append(
            (payment_request.name, stripe_invoice["id"], stripe_invoice["payment_intent"])
        )
        or SimpleNamespace(name="ACC-PAY-0001"),
    )

    reconcile.create_missing_entries_for_paid_requests(
        SimpleNamespace(get_password=lambda fieldname: "sk_live")
    )

    assert created == [
        (
            "PAY-REQ-0001",
            "in_1TbopE2LDx66zyddwQ9RyLdx",
            "pi_3TbopG2LDx66zydd1GOgN5r7",
        )
    ]
    assert fake_frappe.db.set_values == [
        (
            "Payment Request",
            "PAY-REQ-0001",
            "stripe_payment_intent_id",
            "pi_3TbopG2LDx66zydd1GOgN5r7",
            False,
        )
    ]
