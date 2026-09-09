import importlib
import sys
import types
from types import SimpleNamespace


class FakeFrappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self._ = lambda value: value
        self.allowed_role = None
        self.document = None
        self.db = SimpleNamespace()

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def only_for(self, role):
        self.allowed_role = role

    def get_doc(self, doctype, name):
        assert (doctype, name) == ("Payment Request", "ACC-PRQ-2026-00116")
        return self.document

    def throw(self, message):
        raise RuntimeError(message)


def load_utils(monkeypatch):
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
    return importlib.import_module("payments.utils"), fake_frappe


def test_placeholder_country_with_us_state_and_zip_is_treated_as_us(monkeypatch):
    utils, _ = load_utils(monkeypatch)

    assert utils.normalize_customer_country("NA", "AZ", "85641") == "United States"
    assert utils.normalize_customer_country("NA", "", "") is None


def test_retry_creates_one_missing_invoice_for_submitted_unpaid_request(monkeypatch):
    utils, fake_frappe = load_utils(monkeypatch)
    document = SimpleNamespace(
        docstatus=1,
        status="Requested",
        stripe_invoice_id=None,
        stripe_invoice_url=None,
        stripe_payment_status="N/A",
        check_permission=lambda permission: None,
    )

    def create_invoice(doc):
        doc.stripe_invoice_id = "in_123"
        doc.stripe_invoice_url = "https://invoice.stripe.com/i/acct_test/test"
        doc.stripe_payment_status = "Pending"

    document.reload = lambda: None
    fake_frappe.document = document
    monkeypatch.setattr(
        utils,
        "get_stripe_settings",
        lambda: SimpleNamespace(enable_automatic_checkout=True),
    )
    monkeypatch.setattr(utils, "create_stripe_invoice", create_invoice)

    result = utils.retry_missing_stripe_invoice("ACC-PRQ-2026-00116")

    assert fake_frappe.allowed_role == "System Manager"
    assert result == {
        "success": True,
        "invoice_url": "https://invoice.stripe.com/i/acct_test/test",
        "invoice_id": "in_123",
        "status": "Pending",
    }


def test_retry_refuses_to_duplicate_an_existing_invoice(monkeypatch):
    utils, fake_frappe = load_utils(monkeypatch)
    fake_frappe.document = SimpleNamespace(
        docstatus=1,
        status="Requested",
        stripe_invoice_id="in_existing",
        stripe_invoice_url="https://invoice.stripe.com/i/acct_test/existing",
        check_permission=lambda permission: None,
    )

    try:
        utils.retry_missing_stripe_invoice("ACC-PRQ-2026-00116")
    except RuntimeError as error:
        assert str(error) == "A Stripe invoice already exists for this Payment Request"
    else:
        raise AssertionError("Expected duplicate protection to reject the retry")
