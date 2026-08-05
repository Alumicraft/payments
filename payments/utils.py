# Copyright (c) 2026, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_seconds
import json


# Rate limiting: Minimum seconds between invoice creation attempts
RATE_LIMIT_SECONDS = 5
SYNCABLE_EXTERNAL_PAYMENT_STATUSES = ("Pending", "N/A", "")
PAYMENT_REQUEST_AMOUNT_TOLERANCE = 0.01
UNALLOCATED_PAYMENT_AMOUNT_OVERAGE_RATE = 0.10


def to_currency_float(value):
    return round(float(value or 0), 2)


def to_stripe_minor_units(value):
    """Convert the app's two-decimal currency amounts to Stripe minor units."""
    return int(round(to_currency_float(value) * 100))


def should_sync_external_payment_status(stripe_payment_status):
    return not stripe_payment_status or stripe_payment_status in SYNCABLE_EXTERNAL_PAYMENT_STATUSES


def get_payment_status_after_external_payment(stripe_invoice_id=None):
    if stripe_invoice_id:
        return close_stripe_invoice_after_external_payment(stripe_invoice_id)

    return "Paid"


def create_stripe_invoice(doc, method=None):
    """
    Create a Stripe Invoice for a Payment Request.
    Triggered by on_submit hook on Payment Request.

    Args:
        doc: Payment Request document
        method: Hook method name (unused)
    """
    # Check if automatic checkout is enabled
    settings = get_stripe_settings()
    if not settings or not settings.enable_automatic_checkout:
        return

    # Rate limit check
    if is_rate_limited(doc.name):
        frappe.log_error(
            f"Rate limited: Skipping invoice creation for {doc.name}",
            "Stripe Integration"
        )
        return

    # Check if this is an amended document with stale Stripe data
    if doc.amended_from and doc.stripe_invoice_id:
        # Clear stale Stripe fields from the amended document
        doc.stripe_invoice_id = None
        doc.stripe_invoice_url = None
        doc.stripe_payment_status = None
        doc.stripe_payment_intent_id = None
        frappe.log_error(
            f"Cleared stale Stripe data from amended document {doc.name} (amended from {doc.amended_from})",
            "Stripe Integration"
        )

    # Check if invoice already exists (for non-amended documents)
    if doc.stripe_invoice_id:
        frappe.log_error(
            f"Invoice already exists for {doc.name}: {doc.stripe_invoice_id}",
            "Stripe Integration"
        )
        return
    
    try:
        _create_stripe_invoice_internal(doc)
    except Exception as e:
        frappe.log_error(
            f"Error creating Stripe invoice for {doc.name}: {str(e)}",
            "Stripe Integration Error"
        )
        raise


def handle_payment_request_update(doc, method=None):
    """
    Handle updates to Payment Request.
    Triggered by on_update hook.
    
    Checks if card toggle was changed and regenerates invoice if needed.
    """
    sync_paid_payment_request_status(doc)

    # Only process if invoice exists and is pending
    if not doc.stripe_invoice_id:
        return
    
    if doc.stripe_payment_status and doc.stripe_payment_status != "Pending":
        return
    
    # Check if allow_card_payment changed
    old_doc = doc.get_doc_before_save()
    if old_doc and old_doc.allow_card_payment != doc.allow_card_payment:
        # Card toggle changed - need to regenerate invoice
        frappe.log_error(
            f"Card toggle changed for {doc.name}, regenerating invoice",
            "Stripe Integration"
        )
        regenerate_stripe_invoice(doc.name)


def sync_paid_payment_request_status(doc):
    """Keep the custom Stripe payment status aligned when ERPNext marks a request paid."""
    if (
        doc.docstatus == 1
        and doc.status == "Paid"
        and (not hasattr(doc, "outstanding_amount") or doc.outstanding_amount == 0)
        and should_sync_external_payment_status(getattr(doc, "stripe_payment_status", None))
    ):
        stripe_payment_status = get_payment_status_after_external_payment(
            getattr(doc, "stripe_invoice_id", None)
        )
        doc.db_set("stripe_payment_status", stripe_payment_status, update_modified=False)


def close_stripe_invoice_after_external_payment(stripe_invoice_id):
    """
    Close a pending Stripe invoice after ERPNext records payment elsewhere.

    Returns the local Stripe payment status to store on Payment Request.
    """
    import stripe

    settings = get_stripe_settings()
    if not settings:
        return "Paid"

    stripe.api_key = settings.get_password("api_key")

    try:
        invoice = stripe.Invoice.retrieve(stripe_invoice_id)

        if invoice.status == "draft":
            stripe.Invoice.delete(stripe_invoice_id)
            return "Voided"

        if invoice.status == "open":
            stripe.Invoice.void_invoice(stripe_invoice_id)
            return "Voided"

        if invoice.status == "void":
            return "Voided"

        if invoice.status == "paid":
            return "Paid"

    except stripe.error.StripeError as e:
        frappe.log_error(
            f"Failed to close Stripe Invoice {stripe_invoice_id} after external payment: {str(e)}",
            "Stripe Invoice Close Error"
        )

    return "Paid"


def _create_stripe_invoice_internal(doc):
    """
    Internal function to create Stripe Invoice.
    
    Args:
        doc: Payment Request document
    """
    import stripe
    
    settings = get_stripe_settings()
    stripe.api_key = settings.get_password("api_key")
    
    # Validate required fields
    if not doc.grand_total or doc.grand_total <= 0:
        frappe.throw(_("Payment Request must have a valid amount"))
    
    if not doc.email_to:
        frappe.throw(_("Payment Request must have a customer email"))
    
    # Get customer info
    customer = get_erpnext_customer(doc)
    customer_country = get_customer_country(customer) if customer else "US"
    country_lower = (customer_country or "").strip().lower()
    is_us_customer = country_lower in ("us", "united states", "united states of america", "usa")
    
    # Get or create Stripe customer
    stripe_customer_id = get_or_create_stripe_customer(doc, customer, stripe)
    
    # Calculate amounts and payment methods
    base_amount = doc.grand_total
    allow_card = doc.allow_card_payment and is_us_customer  # Cards only for US customers
    fee_rate = (settings.card_fee_rate or 3) / 100  # Get fee rate from settings

    if allow_card:
        card_fee = round(base_amount * fee_rate, 2)
        doc.card_processing_fee = card_fee
        doc.total_with_card_fee = base_amount + card_fee
    else:
        doc.card_processing_fee = 0
        doc.total_with_card_fee = 0
    
    # International customers — skip Stripe, handle wire transfer manually
    if not is_us_customer:
        doc.db_set("stripe_payment_status", "N/A", update_modified=False)
        frappe.msgprint(
            _("International customer — Stripe invoice not created. Handle wire transfer manually."),
            alert=True,
            indicator='orange'
        )
        return

    # Determine payment methods (US customers only at this point)
    payment_method_types = ['us_bank_account']  # ACH Direct Debit
    if allow_card:
        payment_method_types.append('card')

    # Create Stripe Invoice
    try:
        # Build invoice create params
        invoice_params = {
            'customer': stripe_customer_id,
            'collection_method': 'send_invoice',
            'due_date': get_due_date_timestamp(doc),
            'auto_advance': False,  # Don't auto-finalize
            'metadata': {
                'erpnext_payment_request': doc.name,
                'erpnext_customer': customer.name if customer else '',
                'erpnext_invoice_number': doc.reference_name or '',
                'allow_card_payment': '1' if allow_card else '0'
            },
            'payment_settings': {
                'payment_method_types': payment_method_types
            }
        }

        # Create invoice
        invoice = stripe.Invoice.create(**invoice_params)
        
        # Add line item(s)
        description = get_invoice_description(doc)
        currency = doc.currency.lower() if doc.currency else 'usd'

        # Add base amount line item
        stripe.InvoiceItem.create(
            customer=stripe_customer_id,
            invoice=invoice.id,
            amount=to_stripe_minor_units(base_amount),
            currency=currency,
            description=f"Payment for {doc.reference_name}" if doc.reference_name else description,
            metadata={'erpnext_invoice_number': doc.reference_name or ''}
        )

        # Add card processing fee as separate line item if card payments enabled
        if allow_card and doc.card_processing_fee:
            fee_percent = settings.card_fee_rate or 3
            stripe.InvoiceItem.create(
                customer=stripe_customer_id,
                invoice=invoice.id,
                amount=to_stripe_minor_units(doc.card_processing_fee),
                currency=currency,
                description=f"Card Processing Fee ({fee_percent}%)",
                metadata={'fee_type': 'card_processing_fee'}
            )
        
        # Finalize invoice to generate hosted URL
        finalized_invoice = stripe.Invoice.finalize_invoice(invoice.id)
        
        # Update Payment Request with Stripe info
        doc.stripe_invoice_url = finalized_invoice.hosted_invoice_url
        doc.stripe_invoice_id = finalized_invoice.id
        doc.stripe_payment_status = "Pending"
        
        # Update DB without triggering hooks/save recursion
        doc.db_set({
            'stripe_invoice_url': doc.stripe_invoice_url,
            'stripe_invoice_id': doc.stripe_invoice_id,
            'stripe_payment_status': doc.stripe_payment_status 
        })
        
        # Record rate limit timestamp
        set_rate_limit_timestamp(doc.name)
        
        frappe.msgprint(
            _("Stripe Invoice created successfully. <a href='{0}' target='_blank'>View Invoice</a>").format(
                finalized_invoice.hosted_invoice_url
            ),
            alert=True,
            indicator='green'
        )
        
    except stripe.error.StripeError as e:
        frappe.log_error(
            f"Stripe API Error: {str(e)}",
            "Stripe Integration Error"
        )
        frappe.throw(_("Failed to create Stripe invoice: {0}").format(str(e)))


def get_or_create_stripe_customer(doc, customer, stripe):
    """
    Get existing Stripe customer or create new one.
    Uses database locking to prevent race conditions.
    
    Args:
        doc: Payment Request document
        customer: ERPNext Customer document or None
        stripe: Stripe module
    
    Returns:
        str: Stripe Customer ID
    """
    if customer and customer.stripe_customer_id:
        try:
            stripe.Customer.retrieve(customer.stripe_customer_id)
            return customer.stripe_customer_id
        except stripe.error.InvalidRequestError:
            # Customer no longer exists in Stripe - clear stale ID
            customer.stripe_customer_id = None
            customer.save(ignore_permissions=True)
            frappe.log_error(
                f"Cleared stale Stripe customer ID for {customer.name}",
                "Stripe Integration"
            )
    
    customer_email = (doc.email_to or "").split(",")[0].strip()
    customer_name = customer.customer_name if customer else doc.party_name or customer_email

    # Try to find existing Stripe customer by ERPNext customer metadata.
    # Email alone is unreliable because multiple ERPNext customers can share an email.
    if customer:
        try:
            search_query = f'metadata["erpnext_customer"]:"{customer.name}"'
            search_results = stripe.Customer.search(query=search_query, limit=1)
            if search_results.data:
                stripe_customer_id = search_results.data[0].id
                customer.reload()
                if not customer.stripe_customer_id:
                    customer.stripe_customer_id = stripe_customer_id
                    customer.save(ignore_permissions=True)
                return stripe_customer_id
        except Exception as e:
            frappe.log_error(f"Error searching Stripe customers: {str(e)}", "Stripe Integration")
    
    # Create new Stripe customer
    try:
        # Re-check if customer was created by concurrent request
        if customer:
            customer.reload()
            if customer.stripe_customer_id:
                return customer.stripe_customer_id

        # Create new Stripe customer
        stripe_customer = stripe.Customer.create(
            email=customer_email,
            name=customer_name,
            metadata={
                'erpnext_customer': customer.name if customer else '',
                'erpnext_party_name': doc.party_name or ''
            }
        )

        # Save Stripe Customer ID to ERPNext
        if customer:
            customer.stripe_customer_id = stripe_customer.id
            customer.save(ignore_permissions=True)

        return stripe_customer.id
                
    except Exception as e:
        frappe.log_error(f"Error creating Stripe customer: {str(e)}", "Stripe Integration Error")
        raise


def get_stripe_settings():
    """Get Stripe Settings singleton."""
    try:
        settings = frappe.get_single("Stripe Settings")
        if not settings.api_key:
            return None
        return settings
    except Exception:
        return None


def get_erpnext_customer(doc):
    """Get ERPNext Customer from Payment Request."""
    if doc.party_type == "Customer" and doc.party:
        return frappe.get_doc("Customer", doc.party)
    return None


def get_customer_country(customer):
    """Get customer's country from primary address."""
    if not customer:
        return "US"
    
    # Try to get primary billing address
    address = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Customer", "link_name": customer.name, "parenttype": "Address"},
        "parent"
    )
    
    if address:
        country = frappe.db.get_value("Address", address, "country")
        if country and frappe.db.exists("Country", country):
            return country
        return "US"

    # Fall back to territory — check if it's a valid country name
    if customer.territory and frappe.db.exists("Country", customer.territory):
        return customer.territory

    return "US"


def get_due_date_timestamp(doc):
    """
    Get due date timestamp for Stripe Invoice.
    Prioritizes Payment Request due date, then Reference Document due date, then default 30 days.
    Uses UTC consistently to avoid timezone mismatch between Frappe (system tz) and Python (OS tz).
    """
    import calendar
    from datetime import datetime, timedelta

    due_date = None

    # 1. Try Payment Request due date (if exists)
    if hasattr(doc, 'payment_due_date') and doc.payment_due_date:
        due_date = doc.payment_due_date

    # 2. Try Reference Document due date
    elif doc.reference_doctype and doc.reference_name:
        try:
            for field in ['due_date', 'payment_due_date', 'bill_date']:
                val = frappe.db.get_value(doc.reference_doctype, doc.reference_name, field)
                if val:
                    due_date = val
                    break
        except Exception:
            pass

    # Calculate timestamp using UTC consistently
    if due_date:
        dt = get_datetime(due_date)

        # If midnight (just a date), set to end of day
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=23, minute=59, second=59)

        # Use calendar.timegm which always treats naive datetime as UTC
        timestamp = int(calendar.timegm(dt.timetuple()))

        # Stripe requires due_date in the future — compare against current UTC time
        now_utc = int(calendar.timegm(datetime.utcnow().timetuple()))

        if timestamp <= now_utc:
            # Set to tomorrow end-of-day UTC
            tomorrow = datetime.utcnow() + timedelta(days=1)
            tomorrow = tomorrow.replace(hour=23, minute=59, second=59)
            return int(calendar.timegm(tomorrow.timetuple()))

        return timestamp

    # Default: 30 days from now
    future = datetime.utcnow() + timedelta(days=30)
    return int(calendar.timegm(future.timetuple()))


def get_invoice_description(doc):
    """Build invoice line item description."""
    parts = []
    
    if doc.reference_doctype and doc.reference_name:
        parts.append(f"{doc.reference_doctype}: {doc.reference_name}")
    
    if doc.party_name:
        parts.append(f"Customer: {doc.party_name}")
    
    if not parts:
        parts.append(f"Payment Request: {doc.name}")
    
    return " | ".join(parts)


def is_rate_limited(payment_request_name):
    """Check if invoice creation is rate limited."""
    cache_key = f"stripe_invoice_created_{payment_request_name}"
    last_created = frappe.cache().get_value(cache_key)
    
    if last_created:
        last_time = get_datetime(last_created)
        diff = time_diff_in_seconds(now_datetime(), last_time)
        if diff < RATE_LIMIT_SECONDS:
            return True
    
    return False


def set_rate_limit_timestamp(payment_request_name):
    """Set rate limit timestamp for payment request."""
    cache_key = f"stripe_invoice_created_{payment_request_name}"
    frappe.cache().set_value(cache_key, now_datetime(), expires_in_sec=RATE_LIMIT_SECONDS * 2) 


@frappe.whitelist()
def regenerate_stripe_invoice(payment_request_name):
    """
    Void existing invoice and create a new one.
    
    Args:
        payment_request_name: Name of Payment Request
    
    Returns:
        dict: Result with new invoice URL
    """
    import stripe
    
    doc = frappe.get_doc("Payment Request", payment_request_name)
    
    # Check if invoice exists and is pending
    if not doc.stripe_invoice_id:
        frappe.throw(_("No Stripe invoice exists for this Payment Request"))
    
    if doc.stripe_payment_status == "Paid":
        frappe.throw(_("Cannot regenerate invoice - payment already received"))
    
    settings = get_stripe_settings()
    stripe.api_key = settings.get_password("api_key")
    
    try:
        # Cancel existing invoice (delete if draft, void if open)
        existing = stripe.Invoice.retrieve(doc.stripe_invoice_id)
        if existing.status == "draft":
            stripe.Invoice.delete(doc.stripe_invoice_id)
        elif existing.status == "open":
            stripe.Invoice.void_invoice(doc.stripe_invoice_id)

        # Clear existing invoice data
        doc.stripe_invoice_id = None
        doc.stripe_invoice_url = None
        doc.stripe_payment_status = None
        doc.save(ignore_permissions=True)
        
        # Create new invoice
        _create_stripe_invoice_internal(doc)
        
        # Reload to get new values
        doc.reload()
        
        return {
            "success": True,
            "invoice_url": doc.stripe_invoice_url,
            "invoice_id": doc.stripe_invoice_id
        }
        
    except stripe.error.StripeError as e:
        frappe.log_error(f"Error regenerating invoice: {str(e)}", "Stripe Integration Error")
        frappe.throw(_("Failed to regenerate invoice: {0}").format(str(e)))


@frappe.whitelist()
def get_stripe_invoice_status(payment_request_name):
    """
    Get current status of Stripe invoice.
    
    Args:
        payment_request_name: Name of Payment Request
    
    Returns:
        dict: Invoice status info
    """
    import stripe
    
    doc = frappe.get_doc("Payment Request", payment_request_name)
    
    if not doc.stripe_invoice_id:
        return {"status": "no_invoice"}
    
    settings = get_stripe_settings()
    stripe.api_key = settings.get_password("api_key")
    
    try:
        invoice = stripe.Invoice.retrieve(doc.stripe_invoice_id)
        status = get_stripe_object_value(invoice, "status")
        
        return {
            "status": status,
            "amount_due": (get_stripe_object_value(invoice, "amount_due") or 0) / 100,
            "amount_paid": (get_stripe_object_value(invoice, "amount_paid") or 0) / 100,
            "currency": get_stripe_object_value(invoice, "currency"),
            "hosted_invoice_url": get_stripe_object_value(invoice, "hosted_invoice_url"),
            "paid": get_stripe_object_value(invoice, "paid", status == "paid")
        }
        
    except stripe.error.StripeError as e:
        return {"status": "error", "error": str(e)}


def get_stripe_object_value(obj, key, default=None):
    """Read Stripe objects across SDK versions without assuming dict helpers."""
    try:
        return obj[key]
    except (KeyError, TypeError):
        pass

    try:
        return getattr(obj, key)
    except AttributeError:
        return default


@frappe.whitelist()
def close_paid_payment_request_stripe_invoice(payment_request_name):
    """
    Void/delete an unpaid Stripe invoice after ERPNext has already been paid.

    This is intentionally narrow: it only runs for submitted Payment Requests
    whose reference document is already paid/closed or cancelled. It prevents an
    old payment link from collecting duplicate money.
    """
    doc = frappe.get_doc("Payment Request", payment_request_name)

    if doc.docstatus != 1:
        frappe.throw(_("Payment Request must be submitted"))

    if not doc.stripe_invoice_id:
        frappe.throw(_("No Stripe invoice exists for this Payment Request"))

    if not is_reference_paid_or_cancelled(doc):
        frappe.throw(
            _("Reference {0} {1} is not paid or cancelled").format(
                doc.reference_doctype, doc.reference_name
            )
        )

    stripe_payment_status = close_stripe_invoice_after_external_payment(doc.stripe_invoice_id)
    frappe.db.set_value(
        "Payment Request",
        doc.name,
        {
            "status": "Paid" if doc.status != "Cancelled" else doc.status,
            "stripe_payment_status": stripe_payment_status,
        },
        update_modified=False,
    )

    return {
        "payment_request": doc.name,
        "stripe_invoice_id": doc.stripe_invoice_id,
        "stripe_payment_status": stripe_payment_status,
    }


def is_reference_paid_or_cancelled(payment_request):
    if not payment_request.reference_doctype or not payment_request.reference_name:
        return payment_request.status == "Paid"

    if not frappe.db.exists(payment_request.reference_doctype, payment_request.reference_name):
        return False

    if payment_request.reference_doctype == "Sales Invoice":
        values = frappe.db.get_value(
            "Sales Invoice",
            payment_request.reference_name,
            ["docstatus", "outstanding_amount"],
            as_dict=True,
        )
        return values.docstatus == 2 or to_currency_float(values.outstanding_amount) <= 0

    values = frappe.db.get_value(
        payment_request.reference_doctype,
        payment_request.reference_name,
        ["docstatus", "status"],
        as_dict=True,
    )
    return values.docstatus == 2 or values.status in ("Paid", "Closed", "Completed", "Cancelled")


def void_stripe_invoice_on_manual_payment(doc, method=None):
    """
    Void open Stripe Invoices when a Payment Entry is manually submitted.
    Triggered by on_submit hook on Payment Entry.

    Checks if any linked Payment Request has an unresolved payment status and marks
    the request paid because ERPNext has a submitted Payment Entry. Stripe invoice
    cleanup is best-effort and should not be the only path that updates the
    Payment Request status.
    """
    synced_payment_requests = set()

    payment_entry_references = getattr(doc, "references", []) or []

    # Check each reference in the Payment Entry for linked Payment Requests
    for ref in payment_entry_references:
        for pr in get_exact_reference_payment_requests(ref):
            if mark_payment_request_paid_after_payment_entry(pr, doc.name, synced_payment_requests):
                synced_payment_requests.add(pr.name)

        reference_amount = get_payment_entry_reference_amount(ref, doc)
        reference_project = get_payment_entry_reference_project(ref)
        project_matches = require_unambiguous_fallback_match(
            get_project_amount_payment_requests(reference_project, reference_amount),
            "project and amount",
            doc.name,
        )
        for pr in project_matches:
            if mark_payment_request_paid_after_payment_entry(pr, doc.name, synced_payment_requests):
                synced_payment_requests.add(pr.name)

    unallocated_amount = get_payment_entry_unallocated_amount(doc, payment_entry_references)
    unallocated_matches = require_unambiguous_fallback_match(
        get_unallocated_party_payment_requests(doc, unallocated_amount),
        "customer and unallocated amount",
        doc.name,
    )
    for pr in unallocated_matches:
        if mark_payment_request_paid_after_payment_entry(pr, doc.name, synced_payment_requests):
            synced_payment_requests.add(pr.name)


def require_unambiguous_fallback_match(payment_requests, match_type, payment_entry_name):
    """Return a fallback match only when it identifies one Payment Request."""
    if len(payment_requests) <= 1:
        return payment_requests

    candidate_names = ", ".join(sorted(pr.name for pr in payment_requests))
    frappe.log_error(
        (
            f"Payment Entry {payment_entry_name} matched multiple Payment Requests "
            f"by {match_type}; no request was marked paid automatically. "
            f"Candidates: {candidate_names}"
        ),
        "Ambiguous Payment Request Match",
    )
    return []


def get_exact_reference_payment_requests(ref):
    return frappe.get_all(
        "Payment Request",
        filters={
            "reference_doctype": ref.reference_doctype,
            "reference_name": ref.reference_name,
            "docstatus": 1,
        },
        fields=["name", "status", "stripe_invoice_id", "stripe_payment_status"],
    )


def get_payment_entry_reference_amount(ref, doc):
    allocated_amount = getattr(ref, "allocated_amount", None)
    if allocated_amount is not None:
        return to_currency_float(allocated_amount)

    return to_currency_float(getattr(doc, "paid_amount", None))


def get_payment_entry_unallocated_amount(doc, payment_entry_references):
    unallocated_amount = getattr(doc, "unallocated_amount", None)
    if unallocated_amount is not None:
        return to_currency_float(unallocated_amount)

    total_allocated_amount = getattr(doc, "total_allocated_amount", None)
    if total_allocated_amount is None:
        total_allocated_amount = sum(
            to_currency_float(getattr(ref, "allocated_amount", None))
            for ref in payment_entry_references
        )

    return max(
        to_currency_float(getattr(doc, "paid_amount", None)) - to_currency_float(total_allocated_amount),
        0,
    )


def get_payment_entry_reference_project(ref):
    if ref.reference_doctype not in ("Sales Invoice", "Sales Order"):
        return None

    return frappe.db.get_value(ref.reference_doctype, ref.reference_name, "project")


def get_project_amount_payment_requests(project, amount):
    if not project or not amount:
        return []

    return frappe.db.sql(
        """
        select
            pr.name,
            pr.status,
            pr.stripe_invoice_id,
            pr.stripe_payment_status
        from `tabPayment Request` pr
        left join `tabSales Invoice` si
            on pr.reference_doctype = 'Sales Invoice'
            and pr.reference_name = si.name
        left join `tabSales Order` so
            on pr.reference_doctype = 'Sales Order'
            and pr.reference_name = so.name
        where pr.docstatus = 1
          and abs(ifnull(pr.grand_total, 0) - %(amount)s) <= %(tolerance)s
          and coalesce(nullif(pr.project, ''), nullif(si.project, ''), nullif(so.project, '')) = %(project)s
          and (
              ifnull(pr.status, '') != 'Paid'
              or ifnull(pr.stripe_payment_status, '') in ('Pending', 'N/A', '')
          )
        """,
        {
            "project": project,
            "amount": amount,
            "tolerance": PAYMENT_REQUEST_AMOUNT_TOLERANCE,
        },
        as_dict=True,
    )


def get_unallocated_party_payment_requests(doc, unallocated_amount):
    if (
        not unallocated_amount
        or getattr(doc, "party_type", None) != "Customer"
        or not getattr(doc, "party", None)
    ):
        return []

    return frappe.db.sql(
        """
        select
            pr.name,
            pr.status,
            pr.stripe_invoice_id,
            pr.stripe_payment_status
        from `tabPayment Request` pr
        inner join `tabSales Order` so
            on pr.reference_doctype = 'Sales Order'
            and pr.reference_name = so.name
        where pr.docstatus = 1
          and pr.party_type = %(party_type)s
          and pr.party = %(party)s
          and ifnull(pr.grand_total, 0) > 0
          and %(unallocated_amount)s + %(tolerance)s >= ifnull(pr.grand_total, 0)
          and %(unallocated_amount)s <= ifnull(pr.grand_total, 0) * %(upper_bound_multiplier)s + %(tolerance)s
          and (
              ifnull(pr.status, '') != 'Paid'
              or ifnull(pr.stripe_payment_status, '') in ('Pending', 'N/A', '')
          )
          and (
              %(posting_date)s is null
              or so.transaction_date is null
              or %(posting_date)s >= so.transaction_date
          )
        """,
        {
            "party_type": getattr(doc, "party_type", None),
            "party": getattr(doc, "party", None),
            "posting_date": getattr(doc, "posting_date", None),
            "unallocated_amount": unallocated_amount,
            "upper_bound_multiplier": 1 + UNALLOCATED_PAYMENT_AMOUNT_OVERAGE_RATE,
            "tolerance": PAYMENT_REQUEST_AMOUNT_TOLERANCE,
        },
        as_dict=True,
    )


def mark_payment_request_paid_after_payment_entry(pr, payment_entry_name, synced_payment_requests):
    if pr.name in synced_payment_requests:
        return False

    stripe_payment_status = getattr(pr, "stripe_payment_status", None)
    if getattr(pr, "status", None) == "Paid" and not should_sync_external_payment_status(
        stripe_payment_status
    ):
        return False

    values = {"status": "Paid"}
    if should_sync_external_payment_status(stripe_payment_status):
        values["stripe_payment_status"] = get_payment_status_after_external_payment(
            getattr(pr, "stripe_invoice_id", None)
        )

    frappe.db.set_value("Payment Request", pr.name, values, update_modified=False)

    frappe.msgprint(
        f"Payment Request {pr.name} marked paid after Payment Entry {payment_entry_name} was submitted.",
        indicator="green",
        alert=True,
    )
    return True


def void_stripe_invoice_on_cancel(doc, method=None):
    """
    Void the Stripe Invoice when a Payment Request is cancelled.
    Triggered by on_cancel hook on Payment Request.

    Prevents customers from paying an invoice that ERPNext has already cancelled.
    Only voids open/draft invoices — paid or already-voided invoices are skipped.
    """
    if not doc.stripe_invoice_id:
        return

    # Only void if Stripe payment is still pending
    if doc.stripe_payment_status and doc.stripe_payment_status not in ("Pending", ""):
        return

    import stripe

    settings = get_stripe_settings()
    if not settings:
        return

    stripe.api_key = settings.get_password("api_key")

    try:
        invoice = stripe.Invoice.retrieve(doc.stripe_invoice_id)

        if invoice.status in ("open", "draft"):
            if invoice.status == "draft":
                # Draft invoices can't be voided — delete them instead
                stripe.Invoice.delete(doc.stripe_invoice_id)
            else:
                stripe.Invoice.void_invoice(doc.stripe_invoice_id)

            frappe.db.set_value("Payment Request", doc.name,
                "stripe_payment_status", "Voided", update_modified=False)

            frappe.msgprint(f"Stripe Invoice {doc.stripe_invoice_id} has been voided.")
        elif invoice.status == "paid":
            frappe.msgprint(
                f"Stripe Invoice {doc.stripe_invoice_id} is already paid — cannot void. "
                "Consider issuing a refund in Stripe.",
                indicator="orange", alert=True
            )
    except stripe.error.StripeError as e:
        frappe.log_error(
            f"Failed to void Stripe Invoice {doc.stripe_invoice_id}: {str(e)}",
            "Stripe Invoice Void Error"
        )
        frappe.msgprint(
            f"Could not void Stripe Invoice: {str(e)}. Please void it manually in Stripe.",
            indicator="red", alert=True
        )
