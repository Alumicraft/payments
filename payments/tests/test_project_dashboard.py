import importlib
import sys
import types
from pathlib import Path


PAYMENTS_ROOT = Path(__file__).resolve().parents[1]


def test_project_dashboard_preserves_standard_links_and_adds_payment_requests(monkeypatch):
    standard_module = types.ModuleType("erpnext.projects.doctype.project.project_dashboard")
    standard_module.get_data = lambda: {
        "fieldname": "project",
        "transactions": [
            {"label": "Project", "items": ["Task", "Timesheet"]},
            {"label": "Sales", "items": ["Sales Order", "Sales Invoice"]},
        ],
    }
    monkeypatch.setitem(
        sys.modules,
        "erpnext.projects.doctype.project.project_dashboard",
        standard_module,
    )
    sys.modules.pop("payments.dashboard.project", None)

    dashboard = importlib.import_module("payments.dashboard.project")
    data = dashboard.get_data()

    assert data["fieldname"] == "project"
    assert data["transactions"][0]["items"] == ["Task", "Timesheet"]
    assert data["transactions"][1]["items"] == [
        "Sales Order",
        "Sales Invoice",
        "Payment Request",
    ]


def test_project_dashboard_does_not_duplicate_payment_request(monkeypatch):
    standard_module = types.ModuleType("erpnext.projects.doctype.project.project_dashboard")
    standard_module.get_data = lambda: {
        "transactions": [
            {"label": "Sales", "items": ["Payment Request"]},
        ],
    }
    monkeypatch.setitem(
        sys.modules,
        "erpnext.projects.doctype.project.project_dashboard",
        standard_module,
    )
    sys.modules.pop("payments.dashboard.project", None)

    dashboard = importlib.import_module("payments.dashboard.project")

    assert dashboard.get_data()["transactions"][0]["items"] == ["Payment Request"]


def test_payment_hooks_cover_reallocated_entries_and_new_stripe_references():
    hooks = (PAYMENTS_ROOT / "hooks.py").read_text()
    webhook = (PAYMENTS_ROOT / "webhook.py").read_text()

    assert '"on_update_after_submit": "payments.utils.void_stripe_invoice_on_manual_payment"' in hooks
    assert '"Project": "payments.dashboard.project.get_data"' in hooks
    assert '"payment_request": payment_request.name' in webhook
