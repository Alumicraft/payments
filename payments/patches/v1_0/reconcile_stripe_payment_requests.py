import frappe
from frappe.utils import flt

from payments.utils import close_stripe_invoice_after_external_payment, get_stripe_settings
from payments.webhook import (
    create_payment_entry,
    get_customer_payment_amount,
    get_payment_intent_invoice_id,
)


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
            "stripe_invoice_id": ["is", "set"],
            "reference_doctype": "Sales Invoice",
        },
        fields=[
            "name",
            "stripe_invoice_id",
            "stripe_payment_intent_id",
            "reference_name",
            "grand_total",
            "status",
            "stripe_payment_status",
            "allow_card_payment",
            "card_processing_fee",
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

            if has_existing_payment_entry(row):
                continue

            stripe_invoice = stripe.Invoice.retrieve(row.stripe_invoice_id)
            if getattr(stripe_invoice, "status", None) != "paid":
                continue

            stripe_invoice = stripe_object_to_dict(stripe_invoice)

            stripe_invoice = add_missing_payment_intent(stripe, stripe_invoice)

            if not stripe_payment_matches_invoice_balance(row, stripe_invoice, invoice_outstanding):
                continue

            payment_request = frappe.get_doc("Payment Request", row.name)
            payment_entry = create_payment_entry(payment_request, stripe_invoice)

            payment_intent = stripe_invoice.get("payment_intent")
            payment_request_updates = {
                "status": "Paid",
                "stripe_payment_status": "Paid",
            }
            if payment_intent:
                payment_request_updates["stripe_payment_intent_id"] = payment_intent

            frappe.db.set_value(
                "Payment Request",
                row.name,
                payment_request_updates,
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


def add_missing_payment_intent(stripe, stripe_invoice):
    """Attach a matching PaymentIntent ID when Stripe omits it from the invoice."""
    if stripe_invoice.get("payment_intent"):
        return stripe_invoice

    payment_intent = find_matching_payment_intent(stripe, stripe_invoice)
    if payment_intent:
        stripe_invoice["payment_intent"] = payment_intent

    return stripe_invoice


def find_matching_payment_intent(stripe, stripe_invoice):
    invoice_id = stripe_invoice.get("id")
    customer = stripe_invoice.get("customer")
    amount_paid = stripe_invoice.get("amount_paid")
    currency = (stripe_invoice.get("currency") or "").lower()

    if not customer or not amount_paid or not currency:
        return None

    list_args = {"customer": customer, "limit": 100}
    if stripe_invoice.get("created"):
        list_args["created"] = {"gte": stripe_invoice["created"]}

    payment_intents = stripe.PaymentIntent.list(**list_args)
    candidates = []

    for payment_intent_obj in payment_intents.data:
        payment_intent = stripe_object_to_dict(payment_intent_obj)

        if payment_intent.get("status") != "succeeded":
            continue

        if payment_intent.get("amount") != amount_paid:
            continue

        if (payment_intent.get("currency") or "").lower() != currency:
            continue

        linked_invoice_id = get_payment_intent_invoice_id(payment_intent)
        if linked_invoice_id == invoice_id:
            return payment_intent.get("id")

        if linked_invoice_id:
            continue

        candidates.append(payment_intent.get("id"))

    candidates = [candidate for candidate in candidates if candidate]
    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        frappe.log_error(
            f"Found multiple matching PaymentIntents for Stripe Invoice {invoice_id}: {', '.join(candidates)}",
            "Stripe Payment Request Reconciliation",
        )

    return None


def stripe_payment_matches_invoice_balance(payment_request, stripe_invoice, invoice_outstanding):
    amount_paid = flt((stripe_invoice.get("amount_paid") or 0) / 100, 2)
    amount_paid = get_customer_payment_amount(payment_request, amount_paid)

    difference = abs(flt(amount_paid, 2) - flt(invoice_outstanding, 2))
    return flt(difference, 2) <= ROUNDING_TOLERANCE


def stripe_object_to_dict(stripe_object):
    if isinstance(stripe_object, dict):
        return stripe_object

    if hasattr(stripe_object, "to_dict_recursive"):
        return stripe_object.to_dict_recursive()

    return {
        "id": getattr(stripe_object, "id", None),
        "status": getattr(stripe_object, "status", None),
        "amount_paid": getattr(stripe_object, "amount_paid", None),
        "amount": getattr(stripe_object, "amount", None),
        "currency": getattr(stripe_object, "currency", None),
        "customer": getattr(stripe_object, "customer", None),
        "created": getattr(stripe_object, "created", None),
        "invoice": getattr(stripe_object, "invoice", None),
        "payment_intent": getattr(stripe_object, "payment_intent", None),
        "charge": getattr(stripe_object, "charge", None),
        "payment_details": getattr(stripe_object, "payment_details", None),
        "metadata": getattr(stripe_object, "metadata", None),
    }


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
                "docstatus": 1,
            },
        )
    )
