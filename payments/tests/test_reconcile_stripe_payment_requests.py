import importlib
import sys
import types
from types import SimpleNamespace


class FakeDB:
    def __init__(self, outstanding_amount=42207.57):
        self.outstanding_amount = outstanding_amount
        self.set_values = []

    def get_value(self, doctype, name, fieldname):
        assert doctype == "Sales Invoice"
        assert name == "SINV-0001"
        assert fieldname == "outstanding_amount"
        return self.outstanding_amount

    def exists(self, doctype, filters):
        assert doctype == "Payment Entry"
        assert filters == {
            "reference_no": ["in", ["in_1TbopE2LDx66zyddwQ9RyLdx"]],
            "docstatus": 1,
        }
        return False

    def set_value(self, doctype, name, fieldname, value=None, update_modified=False):
        self.set_values.append((doctype, name, fieldname, value, update_modified))


class FakeFrappe(types.ModuleType):
    def __init__(
        self,
        outstanding_amount=42207.57,
        status="Paid",
        stripe_payment_status="Paid",
        allow_card_payment=False,
        card_processing_fee=0,
    ):
        super().__init__("frappe")
        self.db = FakeDB(outstanding_amount=outstanding_amount)
        self._ = lambda value: value
        self.errors = []
        self.row = SimpleNamespace(
            name="PAY-REQ-0001",
            stripe_invoice_id="in_1TbopE2LDx66zyddwQ9RyLdx",
            stripe_payment_intent_id=None,
            reference_name="SINV-0001",
            grand_total=42207.57,
            status=status,
            stripe_payment_status=stripe_payment_status,
            allow_card_payment=allow_card_payment,
            card_processing_fee=card_processing_fee,
        )

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_all(self, doctype, filters=None, fields=None):
        assert doctype == "Payment Request"
        if filters:
            for fieldname in ("status", "stripe_payment_status"):
                if filters.get(fieldname) and filters[fieldname] != getattr(self.row, fieldname):
                    return []

        return [self.row]

    def get_doc(self, doctype, name):
        assert doctype == "Payment Request"
        assert name == "PAY-REQ-0001"
        return SimpleNamespace(
            name=name,
            allow_card_payment=self.row.allow_card_payment,
            card_processing_fee=self.row.card_processing_fee,
        )

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


class FakeStripeInvoiceObject:
    id = "in_1TbopE2LDx66zyddwQ9RyLdx"
    status = "paid"
    amount_paid = 4220757
    currency = "usd"
    customer = "cus_U8x9Yg2kTidTUs"
    created = 1767225600
    payment_intent = None
    charge = None

    def __getattr__(self, name):
        raise AttributeError(name)


class FakeStripeWithoutInvoiceDict(FakeStripe):
    def retrieve_invoice(self, invoice_id):
        assert invoice_id == "in_1TbopE2LDx66zyddwQ9RyLdx"
        return FakeStripeInvoiceObject()


def load_reconcile(monkeypatch, stripe_cls=FakeStripe, **fake_frappe_kwargs):
    fake_frappe = FakeFrappe(**fake_frappe_kwargs)
    fake_stripe = stripe_cls()

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
            {
                "status": "Paid",
                "stripe_payment_status": "Paid",
                "stripe_payment_intent_id": "pi_3TbopG2LDx66zydd1GOgN5r7",
            },
            None,
            False,
        )
    ]


def test_reconciliation_normalizes_stripe_invoice_object_without_to_dict(monkeypatch):
    reconcile, fake_frappe = load_reconcile(
        monkeypatch,
        stripe_cls=FakeStripeWithoutInvoiceDict,
    )
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
            {
                "status": "Paid",
                "stripe_payment_status": "Paid",
                "stripe_payment_intent_id": "pi_3TbopG2LDx66zydd1GOgN5r7",
            },
            None,
            False,
        )
    ]


def test_reconciliation_recovers_pending_request_when_stripe_invoice_is_paid(monkeypatch):
    reconcile, fake_frappe = load_reconcile(
        monkeypatch,
        outstanding_amount=40978.23,
        status="Requested",
        stripe_payment_status="Pending",
        allow_card_payment=False,
        card_processing_fee=1229.35,
    )
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
            {
                "status": "Paid",
                "stripe_payment_status": "Paid",
                "stripe_payment_intent_id": "pi_3TbopG2LDx66zydd1GOgN5r7",
            },
            None,
            False,
        )
    ]
