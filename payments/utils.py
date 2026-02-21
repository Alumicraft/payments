# Copyright (c) 2026, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, time_diff_in_seconds
import json


# Rate limiting: Minimum seconds between invoice creation attempts
RATE_LIMIT_SECONDS = 5


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
            amount=int(base_amount * 100),  # Convert to cents
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
                amount=int(doc.card_processing_fee * 100),  # Convert to cents
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
    
    customer_email = doc.email_to
    customer_name = customer.customer_name if customer else doc.party_name or customer_email
    
    # Try to find existing Stripe customer by email
    try:
        existing_customers = stripe.Customer.list(email=customer_email, limit=1)
        if existing_customers.data:
            stripe_customer_id = existing_customers.data[0].id
            
            # Save to ERPNext Customer if exists
            if customer:
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
        return country or "US"
    
    return customer.territory if customer.territory else "US"


def get_due_date_timestamp(doc):
    """
    Get due date timestamp for Stripe Invoice.
    Prioritizes Payment Request due date, then Reference Document due date, then default 30 days.
    """
    due_date = None
    
    # 1. Try Payment Request due date (if exists)
    if hasattr(doc, 'payment_due_date') and doc.payment_due_date:
        due_date = doc.payment_due_date
        
    # 2. Try Reference Document due date
    elif doc.reference_doctype and doc.reference_name:
        try:
            # Common field names for due date
            for field in ['due_date', 'payment_due_date', 'bill_date']:
                val = frappe.db.get_value(doc.reference_doctype, doc.reference_name, field)
                if val:
                    due_date = val
                    break
        except Exception:
            pass
            
    # Calculate timestamp
    if due_date:
        dt = get_datetime(due_date)
        
        # If midnight (just a date), set to end of day
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=23, minute=59, second=59)
            
        # Stripe requires due_date to be in the future
        # If due date is in the past, set to tomorrow
        if dt <= now_datetime():
            from frappe.utils import add_days
            return int(get_datetime(add_days(now_datetime(), 1)).timestamp())
            
        return int(dt.timestamp())
        
    # Default: 30 days from now
    from frappe.utils import add_days
    return int(get_datetime(add_days(now_datetime(), 30)).timestamp())


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
        # Void existing invoice
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
        
        return {
            "status": invoice.status,
            "amount_due": invoice.amount_due / 100,
            "amount_paid": invoice.amount_paid / 100,
            "currency": invoice.currency,
            "hosted_invoice_url": invoice.hosted_invoice_url,
            "paid": invoice.paid
        }
        
    except stripe.error.StripeError as e:
        return {"status": "error", "error": str(e)}
