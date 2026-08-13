"""Guarded production accounting repairs that preserve ERPNext's audit trail."""

import frappe
from frappe.utils import flt


ALLOWED_ROLES = {"System Manager", "Accounts Manager"}
TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_bool(value):
    if isinstance(value, str):
        return value.lower() in TRUE_VALUES
    return bool(value)


def _as_list(value):
    if isinstance(value, str):
        value = frappe.parse_json(value)
    return list(value or [])


def _require_accounting_manager():
    roles = set(frappe.get_roles())
    if frappe.session.user != "Administrator" and not roles.intersection(ALLOWED_ROLES):
        frappe.throw("System Manager or Accounts Manager role required", frappe.PermissionError)


def _validate_source_order(order, project, customer):
    if order.docstatus != 1:
        frappe.throw(f"Source Sales Order {order.name} is not submitted")
    if order.project != project or order.customer != customer:
        frappe.throw(f"Source Sales Order {order.name} does not match the target project/customer")
    if flt(order.per_delivered) or flt(order.per_billed):
        frappe.throw(f"Source Sales Order {order.name} has delivery or billing activity and cannot be retired")


def _payment_plan(payment_entry, source_orders, customer):
    if payment_entry.docstatus != 1:
        frappe.throw(f"Payment Entry {payment_entry.name} is not submitted")
    if payment_entry.party_type != "Customer" or payment_entry.party != customer:
        frappe.throw(f"Payment Entry {payment_entry.name} does not belong to the target customer")

    sales_order_refs = [
        row for row in payment_entry.references
        if row.reference_doctype == "Sales Order"
    ]
    other_refs = [
        row for row in payment_entry.references
        if row.reference_doctype != "Sales Order"
    ]
    if other_refs:
        frappe.throw(f"Payment Entry {payment_entry.name} has non-Sales-Order allocations")
    if any(row.reference_name not in source_orders for row in sales_order_refs):
        frappe.throw(f"Payment Entry {payment_entry.name} points to an unexpected Sales Order")

    if sales_order_refs:
        amount = flt(sum(flt(row.allocated_amount) for row in sales_order_refs), 2)
        payment_request = next(
            (getattr(row, "payment_request", None) for row in sales_order_refs
             if getattr(row, "payment_request", None)),
            None,
        )
    else:
        amount = flt(payment_entry.unallocated_amount or payment_entry.paid_amount, 2)
        payment_request = None

    if amount <= 0:
        frappe.throw(f"Payment Entry {payment_entry.name} has no advance available to move")

    return {
        "payment_entry": payment_entry.name,
        "amount": amount,
        "payment_request": payment_request,
    }


@frappe.whitelist()
def reassign_sales_order_advances(
    project,
    target_order,
    payment_entries,
    source_orders,
    dry_run=True,
    cancel_source_orders=True,
):
    """Cancel/amend submitted advances onto one final Sales Order.

    This uses normal document cancellation and submission so GL and Payment
    Ledger reversals remain auditable. No ledger row is edited directly.
    """
    _require_accounting_manager()
    dry_run = _as_bool(dry_run)
    cancel_source_orders = _as_bool(cancel_source_orders)
    payment_entry_names = _as_list(payment_entries)
    source_order_names = _as_list(source_orders)

    if not payment_entry_names or not source_order_names:
        frappe.throw("Payment Entries and source Sales Orders are required")

    target = frappe.get_doc("Sales Order", target_order)
    if target.docstatus != 1 or target.project != project:
        frappe.throw("Target Sales Order must be submitted and belong to the project")

    sources = [frappe.get_doc("Sales Order", name) for name in source_order_names]
    for source in sources:
        _validate_source_order(source, project, target.customer)

    payments = [frappe.get_doc("Payment Entry", name) for name in payment_entry_names]
    plan = [
        _payment_plan(payment, set(source_order_names), target.customer)
        for payment in payments
    ]
    total_to_move = flt(sum(row["amount"] for row in plan), 2)
    available = flt(target.grand_total - target.advance_paid, 2)
    if total_to_move > available + 0.01:
        frappe.throw(
            f"Target Sales Order only has {available:,.2f} available; "
            f"payments total {total_to_move:,.2f}"
        )

    result = {
        "dry_run": dry_run,
        "project": project,
        "target_order": target_order,
        "source_orders": source_order_names,
        "payments": plan,
        "total_to_move": total_to_move,
        "amended_payments": [],
        "cancelled_orders": [],
    }
    if dry_run:
        return result

    allocated_before = flt(target.advance_paid, 2)
    for payment, row in zip(payments, plan):
        original_name = payment.name
        amended = frappe.copy_doc(payment)
        payment.flags.ignore_permissions = True
        payment.cancel()

        amended.name = None
        amended.docstatus = 0
        amended.amended_from = original_name
        amended.set("references", [])
        reference = {
            "reference_doctype": "Sales Order",
            "reference_name": target.name,
            "total_amount": target.grand_total,
            "outstanding_amount": max(flt(target.grand_total - allocated_before, 2), 0),
            "allocated_amount": row["amount"],
            "exchange_rate": 1,
        }
        if row["payment_request"]:
            reference["payment_request"] = row["payment_request"]
        amended.append("references", reference)
        amended.flags.ignore_permissions = True
        amended.insert(ignore_permissions=True)
        amended.flags.ignore_permissions = True
        amended.submit()

        allocated_before = flt(allocated_before + row["amount"], 2)
        result["amended_payments"].append({
            "cancelled": original_name,
            "replacement": amended.name,
            "amount": row["amount"],
        })

    if cancel_source_orders:
        for source in sources:
            # Payment Entry cancellation/submission updates the linked Sales
            # Order's modified timestamp. Reload before cancellation so Frappe's
            # optimistic-lock check sees the current document.
            source = frappe.get_doc("Sales Order", source.name)
            _validate_source_order(source, project, target.customer)
            source.flags.ignore_permissions = True
            source.flags.ignore_links = True
            source.cancel()
            result["cancelled_orders"].append(source.name)

    return result
