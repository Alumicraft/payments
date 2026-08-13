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
        self.sql_values = []
        self.reference_projects = {}
        self.project_payment_requests = []
        self.unallocated_payment_requests = []

    def set_value(self, doctype, name, values, update_modified=False):
        self.values.append((doctype, name, values, update_modified))

    def sql(self, query, values=None, as_dict=False):
        self.sql_queries.append(query)
        self.sql_values.append(values)
        assert as_dict is True
        if "pr.party_type = %(party_type)s" in query:
            return self.unallocated_payment_requests
        return self.project_payment_requests

    def get_value(self, doctype, name, fieldname, **kwargs):
        assert fieldname == "project"
        return self.reference_projects.get((doctype, name))


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
        if filters and filters.get("name"):
            return [
                row for row in self.payment_requests
                if row.name == filters["name"]
            ]
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
        paid_amount=500,
        references=[
            SimpleNamespace(
                reference_doctype="Sales Invoice",
                reference_name="SINV-0001",
                allocated_amount=500,
            )
        ],
    )


def test_stripe_minor_units_rounds_currency_instead_of_truncating(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch)

    assert utils.to_stripe_minor_units(0.29) == 29
    assert utils.to_stripe_minor_units(2.01) == 201
    assert utils.to_stripe_minor_units(533.75) == 53375


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


def test_paid_payment_request_update_syncs_na_status_without_stripe_invoice(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    doc = SimpleNamespace(
        docstatus=1,
        status="Paid",
        outstanding_amount=0,
        stripe_invoice_id=None,
        stripe_payment_status="N/A",
        db_set=lambda *args, **kwargs: fake_frappe.db.set_value("Payment Request", "PAY-REQ-PAID", args, kwargs),
    )

    utils.sync_paid_payment_request_status(doc)

    assert fake_stripe.deleted == []
    assert fake_stripe.voided == []
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
        SimpleNamespace(name="PAY-REQ-0001", grand_total=500, stripe_invoice_id="in_123")
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


def test_submitted_payment_entry_marks_na_request_paid_without_stripe_invoice(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "open", settings=None)
    fake_frappe.payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-0004",
            grand_total=500,
            stripe_invoice_id=None,
            stripe_payment_status="N/A",
        )
    ]

    utils.void_stripe_invoice_on_manual_payment(payment_entry())

    assert fake_stripe.deleted == []
    assert fake_stripe.voided == []
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-0004",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]


def test_submitted_payment_entry_marks_request_paid_after_voiding_open_invoice(monkeypatch):
    settings = SimpleNamespace(get_password=lambda fieldname: "sk_test")
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "open", settings)
    fake_frappe.payment_requests = [
        SimpleNamespace(name="PAY-REQ-0002", grand_total=500, stripe_invoice_id="in_456")
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
        SimpleNamespace(name="PAY-REQ-0003", grand_total=500, stripe_invoice_id="in_789")
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


def test_payment_entry_reference_uses_explicit_payment_request_link(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-ORIGIN",
            grand_total=500,
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
        SimpleNamespace(
            name="PAY-REQ-SAME-INVOICE",
            grand_total=500,
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
    ]
    doc = SimpleNamespace(
        name="ACC-PAY-DIRECT",
        paid_amount=500,
        references=[
            SimpleNamespace(
                reference_doctype="Sales Invoice",
                reference_name="SINV-0001",
                allocated_amount=500,
                payment_request="PAY-REQ-ORIGIN",
            )
        ],
    )

    utils.void_stripe_invoice_on_manual_payment(doc)

    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-ORIGIN",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]


def test_payment_entry_does_not_mark_ambiguous_reference_amount_matches(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-REF-1",
            grand_total=500,
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
        SimpleNamespace(
            name="PAY-REQ-REF-2",
            grand_total=500,
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
    ]

    utils.void_stripe_invoice_on_manual_payment(payment_entry())

    assert fake_frappe.db.values == []
    assert fake_frappe.errors == [
        (
            "Payment Entry ACC-PAY-2026-00001 matched multiple Payment Requests "
            "by reference and amount; no request was marked paid automatically. "
            "Candidates: PAY-REQ-REF-1, PAY-REQ-REF-2",
            "Ambiguous Payment Request Match",
        )
    ]


def test_submitted_invoice_payment_marks_matching_sales_order_request_paid(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.db.reference_projects[("Sales Invoice", "SINV-0001")] = "GAR100326"
    fake_frappe.db.project_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-SO",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        )
    ]
    doc = SimpleNamespace(
        name="ACC-PAY-2026-00001",
        paid_amount=0,
        references=[
            SimpleNamespace(
                reference_doctype="Sales Invoice",
                reference_name="SINV-0001",
                allocated_amount=1595,
            )
        ],
    )

    utils.void_stripe_invoice_on_manual_payment(doc)

    assert fake_frappe.db.sql_values == [
        {
            "project": "GAR100326",
            "amount": 1595.0,
            "tolerance": 0.01,
        }
    ]
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-SO",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]


def test_submitted_unallocated_customer_payment_marks_matching_sales_order_request_paid(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.db.unallocated_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-ELL",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        )
    ]
    doc = SimpleNamespace(
        name="ACC-PAY-2026-53185",
        party_type="Customer",
        party="Chris Ellen",
        paid_amount=22800,
        total_allocated_amount=0,
        unallocated_amount=22800,
        posting_date="2026-03-23",
        references=[],
    )

    utils.void_stripe_invoice_on_manual_payment(doc)

    assert fake_frappe.db.sql_values == [
        {
            "party_type": "Customer",
            "party": "Chris Ellen",
            "posting_date": "2026-03-23",
            "unallocated_amount": 22800.0,
            "upper_bound_multiplier": 1.1,
            "tolerance": 0.01,
        }
    ]
    assert fake_frappe.db.values == [
        (
            "Payment Request",
            "PAY-REQ-ELL",
            {"status": "Paid", "stripe_payment_status": "Paid"},
            False,
        )
    ]


def test_submitted_payment_does_not_mark_ambiguous_project_matches_paid(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.db.reference_projects[("Sales Invoice", "SINV-0001")] = "GAR100326"
    fake_frappe.db.project_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-SO-1",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
        SimpleNamespace(
            name="PAY-REQ-SO-2",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
    ]
    doc = SimpleNamespace(
        name="ACC-PAY-AMBIGUOUS-PROJECT",
        paid_amount=1595,
        references=[
            SimpleNamespace(
                reference_doctype="Sales Invoice",
                reference_name="SINV-0001",
                allocated_amount=1595,
            )
        ],
    )

    utils.void_stripe_invoice_on_manual_payment(doc)

    assert fake_frappe.db.values == []
    assert fake_stripe.deleted == []
    assert fake_stripe.voided == []
    assert fake_frappe.errors == [
        (
            "Payment Entry ACC-PAY-AMBIGUOUS-PROJECT matched multiple Payment Requests "
            "by project and amount; no request was marked paid automatically. "
            "Candidates: PAY-REQ-SO-1, PAY-REQ-SO-2",
            "Ambiguous Payment Request Match",
        )
    ]


def test_submitted_payment_does_not_mark_ambiguous_unallocated_matches_paid(monkeypatch):
    utils, fake_frappe, fake_stripe = load_utils(monkeypatch, "paid", settings=None)
    fake_frappe.db.unallocated_payment_requests = [
        SimpleNamespace(
            name="PAY-REQ-CUSTOMER-1",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
        SimpleNamespace(
            name="PAY-REQ-CUSTOMER-2",
            status="Requested",
            stripe_invoice_id=None,
            stripe_payment_status="Pending",
        ),
    ]
    doc = SimpleNamespace(
        name="ACC-PAY-AMBIGUOUS-CUSTOMER",
        party_type="Customer",
        party="Shared Customer",
        paid_amount=1000,
        total_allocated_amount=0,
        unallocated_amount=1000,
        posting_date="2026-08-04",
        references=[],
    )

    utils.void_stripe_invoice_on_manual_payment(doc)

    assert fake_frappe.db.values == []
    assert fake_stripe.deleted == []
    assert fake_stripe.voided == []
    assert fake_frappe.errors == [
        (
            "Payment Entry ACC-PAY-AMBIGUOUS-CUSTOMER matched multiple Payment Requests "
            "by customer and unallocated amount; no request was marked paid automatically. "
            "Candidates: PAY-REQ-CUSTOMER-1, PAY-REQ-CUSTOMER-2",
            "Ambiguous Payment Request Match",
        )
    ]
