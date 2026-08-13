from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_advance_reassignment_preserves_audit_trail_and_is_dry_run_first():
    source = (ROOT / "api/accounting_cleanup.py").read_text()

    assert "def reassign_sales_order_advances(" in source
    assert "dry_run=True" in source
    assert "payment.cancel()" in source
    assert "amended.amended_from = original_name" in source
    assert "amended.submit()" in source
    assert 'reference_doctype": "Sales Order"' in source
    assert "frappe.db.set_value" not in source


def test_advance_reassignment_has_strict_production_guards():
    source = (ROOT / "api/accounting_cleanup.py").read_text()

    assert "System Manager" in source
    assert "Accounts Manager" in source
    assert "has non-Sales-Order allocations" in source
    assert "has delivery or billing activity" in source
    assert "does not match the target project/customer" in source
    assert "only has" in source and "payments total" in source
