import frappe
from frappe.utils import flt

from payments.utils import close_stripe_invoice_after_external_payment, get_stripe_settings
from payments.webhook import create_payment_entry


ROUNDING_TOLERANCE = 0.01


def execute():
    settings = get_stripe_settings()
    if not settings:
        return

    sync_cancelled_pending_requests()
    create_missing_entries_for_paid_requests(settings)


def sync_cancelled_pending_requests():
    payment_requests = frappe.get_all(
        "Payment Request",
        filters={
            "docstatus": 2,
            "stripe_invoice_id": ["is", "set"],
            "stripe_payment_status": "Pending",
        },
        fields=["name", "stripe_invoice_id"],
    )

    for payment_request in payment_requests:
        try:
            stripe_payment_status = close_stripe_invoice_after_external_payment(
                payment_request.stripe_invoice_id
            )
            frappe.db.set_value(
                "Payment Request",
                payment_request.name,
                "stripe_payment_status",
                stripe_payment_status,
                update_modified=False,
            )
        except Exception as e:
            frappe.log_error(
                f"Failed to reconcile cancelled Payment Request {payment_request.name}: {str(e)}",
                "Stripe Payment Request Reconciliation",
            )


def create_missing_entries_for_paid_requests(settings):
    import stripe

    stripe.api_key = settings.get_password("api_key")

    payment_requests = frappe.get_all(
        "Payment Request",
        filters={
            "docstatus": 1,
            "status": "Paid",
            "stripe_payment_status": "Paid",
            "stripe_invoice_id": ["is", "set"],
            "reference_doctype": "Sales Invoice",
        },
        fields=[
            "name",
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            "reference_name",
            "grand_total",
        ],
    )

    for row in payment_requests:
        try:
            invoice_outstanding = flt(
                frappe.db.get_value("Sales Invoice", row.reference_name, "outstanding_amount"),
                2,
            )

            if invoice_outstanding <= ROUNDING_TOLERANCE:
                continue

            if abs(invoice_outstanding - flt(row.grand_total, 2)) > ROUNDING_TOLERANCE:
                continue

            if has_existing_payment_entry(row):
                continue

            stripe_invoice = stripe.Invoice.retrieve(row.stripe_invoice_id)
            if getattr(stripe_invoice, "status", None) != "paid":
                continue

            if hasattr(stripe_invoice, "to_dict_recursive"):
                stripe_invoice = stripe_invoice.to_dict_recursive()

            payment_request = frappe.get_doc("Payment Request", row.name)
            payment_entry = create_payment_entry(payment_request, stripe_invoice)

            payment_intent = stripe_invoice.get("payment_intent")
            if payment_intent:
                frappe.db.set_value(
                    "Payment Request",
                    row.name,
                    "stripe_payment_intent_id",
                    payment_intent,
                    update_modified=False,
                )

            frappe.log_error(
                f"Created missing Payment Entry {payment_entry.name if payment_entry else None} "
                f"for paid Payment Request {row.name}",
                "Stripe Payment Request Reconciliation",
            )
        except Exception as e:
            frappe.log_error(
                f"Failed to create missing Payment Entry for Payment Request {row.name}: {str(e)}",
                "Stripe Payment Request Reconciliation",
            )


def has_existing_payment_entry(payment_request):
    reference_numbers = [
        value
        for value in (
            payment_request.stripe_payment_intent_id,
            payment_request.stripe_invoice_id,
        )
        if value
    ]

    if not reference_numbers:
        return False

    return bool(
        frappe.db.exists(
            "Payment Entry",
            {
                "reference_no": ["in", reference_numbers],
                "docstatus": ["!=", 2],
            },
        )
    )
