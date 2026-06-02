import json

import frappe
from frappe.utils import flt, today


DEFAULT_SMALL_BALANCE_LIMIT = 1.00


@frappe.whitelist()
def write_off_small_sales_invoice_balances(dry_run=True, limit=None):
    """Write off small positive Sales Invoice residual balances."""
    dry_run = coerce_bool(dry_run)
    return _write_off_small_sales_invoice_balances(dry_run=dry_run, limit=limit)


def auto_write_off_small_sales_invoice_balances():
    """Scheduler/patch entrypoint for automatic small residual cleanup."""
    return _write_off_small_sales_invoice_balances(dry_run=False)


def _write_off_small_sales_invoice_balances(dry_run=True, limit=None):
    settings = frappe.get_single("Stripe Settings")
    limit = get_small_balance_limit(settings, limit)
    results = {
        "dry_run": dry_run,
        "limit": limit,
        "total": 0,
        "would_write_off": 0,
        "written_off": 0,
        "skipped": 0,
        "errors": [],
    }

    if limit <= 0:
        frappe.log_error(json.dumps(results, indent=2), "Small Balance Write-Off Results")
        return results

    invoices = get_small_balance_invoices(limit)
    results["total"] = len(invoices)

    for invoice in invoices:
        try:
            amount = flt(invoice.outstanding_amount, 2)
            if should_skip_invoice(invoice, amount, limit):
                results["skipped"] += 1
                continue

            write_off_account = get_small_balance_write_off_account(settings, invoice.company)
            if not write_off_account:
                results["skipped"] += 1
                results["errors"].append({
                    "invoice": invoice.name,
                    "error": f"No small balance write-off account configured for {invoice.company}",
                })
                continue

            item = {
                "invoice": invoice.name,
                "customer": invoice.customer,
                "company": invoice.company,
                "amount": amount,
                "account": write_off_account,
            }

            if dry_run:
                results["would_write_off"] += 1
                continue

            item["journal_entry"] = create_small_balance_write_off(invoice, amount, write_off_account)
            results["written_off"] += 1
        except Exception as e:
            results["errors"].append({"invoice": getattr(invoice, "name", None), "error": str(e)})

    frappe.log_error(json.dumps(results, indent=2), "Small Balance Write-Off Results")
    return results


def get_small_balance_invoices(limit):
    return frappe.get_all(
        "Sales Invoice",
        filters=[
            ["docstatus", "=", 1],
            ["outstanding_amount", ">", 0],
            ["outstanding_amount", "<=", limit],
        ],
        fields=[
            "name",
            "customer",
            "company",
            "debit_to",
            "grand_total",
            "outstanding_amount",
        ],
        order_by="posting_date asc, name asc",
        limit_page_length=500,
    )


def should_skip_invoice(invoice, amount, limit):
    if amount <= 0 or amount > limit:
        return True

    grand_total = flt(getattr(invoice, "grand_total", 0), 2)
    if grand_total <= 0:
        return True

    if flt(grand_total - amount, 2) <= 0:
        return True

    return False


def create_small_balance_write_off(invoice, amount, write_off_account):
    if not getattr(invoice, "debit_to", None):
        raise ValueError(f"Sales Invoice {invoice.name} has no receivable account")

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = invoice.company
    je.posting_date = today()
    je.user_remark = f"Small balance write-off for Sales Invoice {invoice.name}"

    je.append("accounts", {
        "account": write_off_account,
        "debit_in_account_currency": amount,
        "credit_in_account_currency": 0,
        "user_remark": f"Small balance write-off for {invoice.name}",
    })

    je.append("accounts", {
        "account": invoice.debit_to,
        "party_type": "Customer",
        "party": invoice.customer,
        "reference_type": "Sales Invoice",
        "reference_name": invoice.name,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": amount,
        "user_remark": "Clear small outstanding balance",
    })

    je.insert(ignore_permissions=True)
    je.submit()
    return je.name


def get_small_balance_limit(settings, limit):
    if limit is not None and limit != "":
        return flt(limit, 2)

    configured_limit = get_doc_value(settings, "small_balance_write_off_limit")
    if configured_limit is not None and configured_limit != "":
        return flt(configured_limit, 2)

    return DEFAULT_SMALL_BALANCE_LIMIT


def get_small_balance_write_off_account(settings, company):
    configured_account = get_doc_value(settings, "small_balance_write_off_account")
    if configured_account:
        return configured_account

    for fieldname in ("round_off_account", "write_off_account", "exchange_gain_loss_account"):
        try:
            account = frappe.db.get_value("Company", company, fieldname)
        except Exception:
            continue

        if account:
            return account

    return None


def get_doc_value(doc, fieldname):
    if hasattr(doc, "get"):
        return doc.get(fieldname)

    return getattr(doc, fieldname, None)


def coerce_bool(value):
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "")

    return bool(value)
