# Copyright (c) 2026, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import now_datetime
import json


@frappe.whitelist(allow_guest=True)
def handle_stripe_webhook():
    """
    Handle incoming Stripe webhook events.
    
    This endpoint receives webhook events from Stripe and processes them accordingly.
    It verifies the webhook signature, checks for idempotency, and processes the event.
    
    Endpoint: /api/method/payments.webhook.handle_stripe_webhook
    """
    from payments.utils import get_gateway_controller
    from payments.payments.doctype.stripe_webhook_event.stripe_webhook_event import (
        create_stripe_webhook_event,
    )
    import stripe
    
    # Get raw request body
    payload = frappe.request.get_data(as_text=True)
    sig_header = frappe.request.headers.get('Stripe-Signature')
    
    if not payload:
        frappe.throw(_("No payload received"), frappe.ValidationError)
    
    # Get Stripe settings
    try:
        settings = frappe.get_single("Stripe Settings")
        webhook_secret = settings.get_password("webhook_secret")
        stripe.api_key = settings.get_password("api_key")
    except Exception as e:
        frappe.log_error(f"Failed to get Stripe settings: {str(e)}", "Stripe Webhook Error")
        return {"status": "error", "message": "Configuration error"}
    
    # Verify webhook signature
    event = None
    if webhook_secret and sig_header:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, webhook_secret
            )
        except stripe.error.SignatureVerificationError as e:
            frappe.log_error(f"Webhook signature verification failed: {str(e)}", "Stripe Webhook Error")
            frappe.throw(_("Invalid webhook signature"), frappe.AuthenticationError)
    else:
        # If no webhook secret configured, parse payload directly (not recommended for production)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            frappe.throw(_("Invalid JSON payload"), frappe.ValidationError)
    
    event_id = event.get('id')
    event_type = event.get('type')
    
    frappe.log_error(f"Received Stripe webhook: {event_type} ({event_id})", "Stripe Webhook")
    
    # Idempotency check - skip if already processed
    if is_event_processed(event_id):
        frappe.log_error(f"Event already processed: {event_id}", "Stripe Webhook")
        return {"status": "already_processed", "event_id": event_id}
    
    # Record event before processing (for idempotency)
    webhook_event_doc = record_webhook_event(event)
    
    try:
        # Process event based on type
        result = process_event(event, event_type)
        
        # Update webhook event status
        webhook_event_doc.status = "Success"
        webhook_event_doc.save(ignore_permissions=True)
        frappe.db.commit()
        
        return {"status": "success", "event_id": event_id, "result": result}
        
    except Exception as e:
        # Record error
        webhook_event_doc.status = "Failed"
        webhook_event_doc.error_message = str(e)
        webhook_event_doc.save(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(f"Error processing webhook {event_id}: {str(e)}", "Stripe Webhook Error")
        return {"status": "error", "event_id": event_id, "error": str(e)}


def is_event_processed(event_id):
    """Check if a Stripe event has already been processed."""
    return frappe.db.exists("Stripe Webhook Event", {"event_id": event_id})


def record_webhook_event(event):
    """
    Record webhook event for idempotency tracking.
    
    Args:
        event: Stripe event object
    
    Returns:
        Stripe Webhook Event document
    """
    event_id = event.get('id')
    event_type = event.get('type')
    
    # Extract invoice/payment info if available
    data = event.get('data', {}).get('object', {})
    invoice_id = data.get('id') if event_type.startswith('invoice.') else data.get('invoice')
    amount = data.get('amount_paid', data.get('amount', 0))
    currency = data.get('currency', 'usd')
    
    # Find related Payment Request
    payment_request = None
    metadata = data.get('metadata', {})
    if 'erpnext_payment_request' in metadata:
        payment_request = metadata['erpnext_payment_request']
    elif invoice_id:
        # Look up by invoice ID
        payment_request = frappe.db.get_value(
            "Payment Request",
            {"stripe_invoice_id": invoice_id},
            "name"
        )
    
    doc = frappe.get_doc({
        "doctype": "Stripe Webhook Event",
        "event_id": event_id,
        "event_type": event_type,
        "processed_at": now_datetime(),
        "status": "Success",  # Will be updated if processing fails
        "payment_request": payment_request,
        "stripe_invoice_id": invoice_id,
        "amount": (amount / 100) if amount else 0,  # Convert from cents
        "currency": currency.upper() if currency else "",
        "raw_payload": json.dumps(event, indent=2)[:10000]  # Limit size
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    
    return doc


def process_event(event, event_type):
    """
    Process Stripe event based on type.
    
    Args:
        event: Stripe event object
        event_type: Event type string
    
    Returns:
        dict: Processing result
    """
    handlers = {
        'invoice.paid': handle_invoice_paid,
        'invoice.payment_failed': handle_invoice_payment_failed,
        'invoice.voided': handle_invoice_voided,
        'invoice.payment_action_required': handle_invoice_action_required,
        'payment_intent.succeeded': handle_payment_intent_succeeded,
    }
    
    handler = handlers.get(event_type)
    if handler:
        return handler(event)
    else:
        return {"message": f"Unhandled event type: {event_type}"}


def handle_invoice_paid(event):
    """
    Handle invoice.paid event.
    Creates Payment Entry in ERPNext.
    
    Args:
        event: Stripe event object
    
    Returns:
        dict: Processing result
    """
    invoice = event.get('data', {}).get('object', {})
    invoice_id = invoice.get('id')
    
    # Find Payment Request
    payment_request_name = find_payment_request(invoice)
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}
    
    payment_request = frappe.get_doc("Payment Request", payment_request_name)
    
    # Check if already marked as paid
    if payment_request.stripe_payment_status == "Paid":
        return {"message": f"Payment Request {payment_request_name} already marked as paid"}
    
    # Update Payment Request status
    payment_request.stripe_payment_status = "Paid"
    payment_request.stripe_payment_intent_id = invoice.get('payment_intent')
    payment_request.save(ignore_permissions=True)
    
    # Create Payment Entry
    try:
        payment_entry = create_payment_entry(payment_request, invoice)
        frappe.db.commit()
        
        return {
            "message": "Payment recorded successfully",
            "payment_request": payment_request_name,
            "payment_entry": payment_entry.name if payment_entry else None
        }
    except Exception as e:
        frappe.log_error(
            f"Failed to create Payment Entry for {payment_request_name}: {str(e)}",
            "Stripe Webhook Error"
        )
        return {
            "message": f"Status updated but Payment Entry creation failed: {str(e)}",
            "payment_request": payment_request_name
        }


def handle_invoice_payment_failed(event):
    """Handle invoice.payment_failed event."""
    invoice = event.get('data', {}).get('object', {})
    invoice_id = invoice.get('id')
    
    payment_request_name = find_payment_request(invoice)
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}
    
    # Update status only - no follow-up emails
    frappe.db.set_value(
        "Payment Request",
        payment_request_name,
        "stripe_payment_status",
        "Failed"
    )
    
    # Log failure reason
    failure_message = invoice.get('last_finalization_error', {}).get('message', 'Unknown error')
    frappe.log_error(
        f"Payment failed for {payment_request_name}: {failure_message}",
        "Stripe Payment Failed"
    )
    
    return {"message": f"Payment Request {payment_request_name} marked as failed"}


def handle_invoice_voided(event):
    """Handle invoice.voided event."""
    invoice = event.get('data', {}).get('object', {})
    invoice_id = invoice.get('id')
    
    payment_request_name = find_payment_request(invoice)
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}
    
    frappe.db.set_value(
        "Payment Request",
        payment_request_name,
        "stripe_payment_status",
        "Voided"
    )
    
    return {"message": f"Payment Request {payment_request_name} marked as voided"}


def handle_invoice_action_required(event):
    """Handle invoice.payment_action_required event."""
    invoice = event.get('data', {}).get('object', {})
    invoice_id = invoice.get('id')
    
    payment_request_name = find_payment_request(invoice)
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}
    
    # Update status only - no follow-up emails
    frappe.db.set_value(
        "Payment Request",
        payment_request_name,
        "stripe_payment_status",
        "Action Required"
    )
    
    return {"message": f"Payment Request {payment_request_name} marked as action required"}


def handle_payment_intent_succeeded(event):
    """
    Handle payment_intent.succeeded event.
    Backup reconciliation if invoice events are delayed.
    
    Args:
        event: Stripe event object
    
    Returns:
        dict: Processing result
    """
    payment_intent = event.get('data', {}).get('object', {})
    invoice_id = payment_intent.get('invoice')
    
    if not invoice_id:
        # Not related to an invoice
        return {"message": "Payment intent not linked to invoice"}
    
    # Find Payment Request by invoice ID
    payment_request_name = frappe.db.get_value(
        "Payment Request",
        {"stripe_invoice_id": invoice_id},
        "name"
    )
    
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}
    
    payment_request = frappe.get_doc("Payment Request", payment_request_name)
    
    # Only process if not already paid (invoice.paid should have handled it)
    if payment_request.stripe_payment_status == "Paid":
        return {"message": f"Payment Request {payment_request_name} already paid via invoice.paid event"}
    
    # Update status and create payment entry
    payment_request.stripe_payment_status = "Paid"
    payment_request.stripe_payment_intent_id = payment_intent.get('id')
    payment_request.save(ignore_permissions=True)
    
    # Fetch invoice for payment entry creation
    import stripe
    settings = frappe.get_single("Stripe Settings")
    stripe.api_key = settings.get_password("api_key")
    
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        payment_entry = create_payment_entry(payment_request, invoice)
        frappe.db.commit()
        
        return {
            "message": "Payment recorded via payment_intent.succeeded",
            "payment_request": payment_request_name,
            "payment_entry": payment_entry.name if payment_entry else None
        }
    except Exception as e:
        frappe.log_error(
            f"Backup payment processing failed for {payment_request_name}: {str(e)}",
            "Stripe Webhook Error"
        )
        return {"message": f"Status updated but Payment Entry creation failed: {str(e)}"}


def find_payment_request(invoice):
    """
    Find Payment Request for a Stripe invoice.
    
    Args:
        invoice: Stripe invoice object
    
    Returns:
        str: Payment Request name or None
    """
    # Try metadata first
    metadata = invoice.get('metadata', {})
    if 'erpnext_payment_request' in metadata:
        name = metadata['erpnext_payment_request']
        if frappe.db.exists("Payment Request", name):
            return name
    
    # Fall back to invoice ID lookup
    invoice_id = invoice.get('id')
    return frappe.db.get_value(
        "Payment Request",
        {"stripe_invoice_id": invoice_id},
        "name"
    )


def create_payment_entry(payment_request, invoice):
    """
    Create Payment Entry for a paid invoice.
    
    Args:
        payment_request: Payment Request document
        invoice: Stripe invoice object
    
    Returns:
        Payment Entry document or None
    """
    # Check if Payment Entry already exists (idempotency)
    existing = frappe.db.exists(
        "Payment Entry",
        {
            "reference_no": invoice.get('payment_intent') or invoice.get('id'),
            "docstatus": ["!=", 2]  # Not cancelled
        }
    )
    if existing:
        frappe.log_error(
            f"Payment Entry already exists: {existing}",
            "Stripe Webhook"
        )
        return frappe.get_doc("Payment Entry", existing)
    
    # Get amount in proper currency
    amount_paid = invoice.get('amount_paid', 0) / 100  # Convert from cents
    currency = invoice.get('currency', 'usd').upper()
    
    # Get company from Payment Request
    company = payment_request.company or frappe.defaults.get_user_default("Company")
    
    if not company:
        frappe.throw(_("Company not found for Payment Entry"))
    
    # Get payment accounts
    mode_of_payment = "Stripe"
    
    # Check if Stripe mode of payment exists, if not use Bank
    if not frappe.db.exists("Mode of Payment", "Stripe"):
        mode_of_payment = "Bank Draft"  # Fallback
    
    # Get account from Mode of Payment Account
    payment_account = frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mode_of_payment, "company": company},
        "default_account"
    )
    
    if not payment_account:
        # Try to get default bank account
        payment_account = frappe.db.get_value(
            "Company",
            company,
            "default_bank_account"
        )
    
    if not payment_account:
        frappe.log_error(
            f"No payment account found for company {company}",
            "Stripe Webhook Error"
        )
        return None
    
    # Create Payment Entry
    try:
        pe = frappe.get_doc({
            "doctype": "Payment Entry",
            "payment_type": "Receive",
            "party_type": payment_request.party_type,
            "party": payment_request.party,
            "company": company,
            "paid_from": get_receivable_account(company),
            "paid_to": payment_account,
            "paid_amount": amount_paid,
            "received_amount": amount_paid,
            "source_exchange_rate": 1,
            "target_exchange_rate": 1,
            "reference_no": invoice.get('payment_intent') or invoice.get('id'),
            "reference_date": now_datetime(),
            "mode_of_payment": mode_of_payment,
            "remarks": f"Payment received via Stripe Invoice {invoice.get('id')}"
        })
        
        # Add reference to original document if available
        if payment_request.reference_doctype and payment_request.reference_name:
            pe.append("references", {
                "reference_doctype": payment_request.reference_doctype,
                "reference_name": payment_request.reference_name,
                "allocated_amount": amount_paid
            })
        
        pe.insert(ignore_permissions=True)
        pe.submit()
        
        frappe.log_error(
            f"Created Payment Entry {pe.name} for Payment Request {payment_request.name}",
            "Stripe Webhook"
        )
        
        return pe
        
    except Exception as e:
        frappe.log_error(
            f"Failed to create Payment Entry: {str(e)}",
            "Stripe Webhook Error"
        )
        raise


def get_receivable_account(company):
    """Get default receivable account for company."""
    account = frappe.db.get_value(
        "Company",
        company,
        "default_receivable_account"
    )
    
    if not account:
        # Try to find any receivable account
        account = frappe.db.get_value(
            "Account",
            {"company": company, "account_type": "Receivable", "is_group": 0},
            "name"
        )
    
    return account
