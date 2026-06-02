import importlib
import json
import sys
import types
from types import SimpleNamespace


class FakeJournalEntry:
    def __init__(self, frappe):
        self.frappe = frappe
        self.accounts = []
        self.name = "JV-0001"

    def append(self, table, row):
        assert table == "accounts"
        self.accounts.append(row)

    def insert(self, ignore_permissions=False):
        assert ignore_permissions is True
        self.frappe.inserted.append(self)

    def submit(self):
        self.frappe.submitted.append(self)


class FakeDB:
    def __init__(self):
        self.company_values = {
            ("Alumicraft", "round_off_account"): "Rounding Difference - A",
        }

    def get_value(self, doctype, name, fieldname):
        if doctype == "Company":
            return self.company_values.get((name, fieldname))

        raise AssertionError(f"Unexpected get_value: {doctype}, {name}, {fieldname}")


class FakeFrappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self._ = lambda value: value
        self.db = FakeDB()
        self.inserted = []
        self.submitted = []
        self.errors = []
        self.sales_invoices = [
            SimpleNamespace(
                name="SINV-0001",
                customer="Kevin McMullen",
                company="Alumicraft",
                debit_to="Accounts Receivable - A",
                grand_total=4888.44,
                outstanding_amount=0.01,
            ),
            SimpleNamespace(
                name="SINV-0002",
                customer="Jerry LeClaire",
                company="Alumicraft",
                debit_to="Accounts Receivable - A",
                grand_total=4910.44,
                outstanding_amount=20.00,
            ),
            SimpleNamespace(
                name="SINV-0003",
                customer="Small Invoice",
                company="Alumicraft",
                debit_to="Accounts Receivable - A",
                grand_total=1.01,
                outstanding_amount=1.00,
            ),
        ]

    def whitelist(self, *args, **kwargs):
        def decorator(fn):
            return fn

        return decorator

    def get_single(self, doctype):
        assert doctype == "Stripe Settings"
        return SimpleNamespace(
            small_balance_write_off_limit=1,
            small_balance_write_off_account=None,
        )

    def get_all(self, doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        assert doctype == "Sales Invoice"
        return [
            invoice
            for invoice in self.sales_invoices
            if invoice.outstanding_amount <= 1
        ]

    def new_doc(self, doctype):
        assert doctype == "Journal Entry"
        return FakeJournalEntry(self)

    def log_error(self, message, title=None):
        json.loads(message)
        self.errors.append((message, title))


def load_residuals(monkeypatch):
    fake_frappe = FakeFrappe()
    monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
    monkeypatch.setitem(
        sys.modules,
        "frappe.utils",
        SimpleNamespace(
            flt=lambda value, precision=2: round(float(value or 0), precision),
            today=lambda: "2026-06-02",
        ),
    )

    sys.modules.pop("payments.residuals", None)
    return importlib.import_module("payments.residuals"), fake_frappe


def test_small_balance_write_off_creates_journal_entry_for_one_dollar_or_less(monkeypatch):
    residuals, fake_frappe = load_residuals(monkeypatch)

    result = residuals.write_off_small_sales_invoice_balances(dry_run=False)

    assert result["written_off"] == 2
    assert result["skipped"] == 0
    assert len(fake_frappe.submitted) == 2

    journal_entry = fake_frappe.submitted[0]
    assert journal_entry.voucher_type == "Journal Entry"
    assert journal_entry.company == "Alumicraft"
    assert journal_entry.posting_date == "2026-06-02"
    assert journal_entry.accounts == [
        {
            "account": "Rounding Difference - A",
            "debit_in_account_currency": 0.01,
            "credit_in_account_currency": 0,
            "user_remark": "Small balance write-off for SINV-0001",
        },
        {
            "account": "Accounts Receivable - A",
            "party_type": "Customer",
            "party": "Kevin McMullen",
            "reference_type": "Sales Invoice",
            "reference_name": "SINV-0001",
            "debit_in_account_currency": 0,
            "credit_in_account_currency": 0.01,
            "user_remark": "Clear small outstanding balance",
        },
    ]

    small_invoice_entry = fake_frappe.submitted[1]
    assert small_invoice_entry.accounts[0]["debit_in_account_currency"] == 1.00
    assert small_invoice_entry.accounts[1]["reference_name"] == "SINV-0003"


def test_small_balance_write_off_dry_run_does_not_create_journal_entry(monkeypatch):
    residuals, fake_frappe = load_residuals(monkeypatch)

    result = residuals.write_off_small_sales_invoice_balances(dry_run=True)

    assert result["would_write_off"] == 2
    assert fake_frappe.submitted == []
