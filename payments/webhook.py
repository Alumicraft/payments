# Copyright (c) 2026, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, now_datetime
import json


ROUNDING_TOLERANCE = 0.01


@frappe.whitelist(allow_guest=True)
def handle_stripe_webhook():
    """
    Handle incoming Stripe webhook events.
    
    This endpoint receives webhook events from Stripe and processes them accordingly.
    It verifies the webhook signature, checks for idempotency, and processes the event.
    
    Endpoint: /api/method/payments.webhook.handle_stripe_webhook
    """
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
    
    # Run as Administrator after signature is verified
    frappe.set_user("Administrator")

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
        
        frappe.log_error(str(e), f"Webhook Error: {event_id}")
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
    Creates Payment Entry in ERPNext and records Stripe fees.

    Args:
        event: Stripe event object

    Returns:
        dict: Processing result
    """
    import stripe

    invoice = event.get('data', {}).get('object', {})
    invoice_id = invoice.get('id')

    # Find Payment Request
    payment_request_name = find_payment_request(invoice)
    if not payment_request_name:
        return {"message": f"No Payment Request found for invoice {invoice_id}"}

    payment_request = frappe.get_doc("Payment Request", payment_request_name)

    # Check if already recorded in accounting. A prior webhook may have marked
    # the request paid before Payment Entry creation completed.
    if payment_request.stripe_payment_status == "Paid" and payment_entry_exists_for_invoice(invoice):
        return {"message": f"Payment Request {payment_request_name} already marked as paid"}

    # Don't overwrite Cancelled status — payment was collected but request was cancelled in ERPNext
    if payment_request.status == "Cancelled":
        frappe.db.set_value("Payment Request", payment_request_name, {
            "stripe_payment_status": "Paid",
            "stripe_payment_intent_id": invoice.get('payment_intent')
        }, update_modified=False)
        frappe.log_error(
            f"Stripe payment received for cancelled Payment Request {payment_request_name} (Invoice: {invoice_id}). "
            "Review and refund if needed.",
            "Stripe Payment on Cancelled Request"
        )
        frappe.db.commit()
        return {"message": f"Payment Request {payment_request_name} is cancelled but Stripe collected payment — logged for review"}

    # Update Payment Request status (use set_value for submitted docs)
    frappe.db.set_value("Payment Request", payment_request_name, {
        "status": "Paid",
        "stripe_payment_status": "Paid",
        "stripe_payment_intent_id": invoice.get('payment_intent')
    }, update_modified=False)

    # Create Payment Entry
    result = {
        "message": "Payment recorded successfully",
        "payment_request": payment_request_name
    }

    try:
        # Fetch Stripe fee from charge/balance transaction BEFORE creating Payment Entry
        stripe_fee = 0
        charge_id = invoice.get('charge')

        if charge_id:
            try:
                settings = frappe.get_single("Stripe Settings")
                stripe.api_key = settings.get_password("api_key")

                charge = stripe.Charge.retrieve(charge_id)
                if charge.balance_transaction:
                    balance_txn = stripe.BalanceTransaction.retrieve(charge.balance_transaction)
                    stripe_fee = balance_txn.fee / 100  # Convert cents to dollars
            except Exception as e:
                frappe.log_error(f"Failed to fetch Stripe fee: {str(e)}", "Stripe Webhook")

        payment_entry = create_payment_entry(payment_request, invoice, stripe_fee=stripe_fee)
        result["payment_entry"] = payment_entry.name if payment_entry else None

        # Record Stripe fee as expense (if fee > 0 and accounts configured)
        if stripe_fee > 0:
            try:
                fee_entry = record_stripe_fee(payment_request, stripe_fee, invoice_id)
                result["fee_journal_entry"] = fee_entry
            except Exception as e:
                frappe.log_error(f"Failed to record Stripe fee: {str(e)}", "Stripe Webhook Error")

        # Record card fee income if customer paid surcharge
        if payment_request.card_processing_fee:
            try:
                income_entry = record_card_fee_income(payment_request, payment_request.card_processing_fee, invoice_id)
                result["card_fee_journal_entry"] = income_entry
            except Exception as e:
                frappe.log_error(f"Failed to record card fee income: {str(e)}", "Stripe Webhook Error")

        frappe.db.commit()
        return result

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
    frappe.db.set_value("Payment Request", payment_request_name, {
        "status": "Failed",
        "stripe_payment_status": "Failed"
    }, update_modified=False)
    
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
    
    frappe.db.set_value("Payment Request", payment_request_name, {
        "status": "Cancelled",
        "stripe_payment_status": "Voided"
    }, update_modified=False)
    
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
    invoice_id = get_payment_intent_invoice_id(payment_intent)
    
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
    
    # Only skip if accounting is already complete. invoice.paid may have marked
    # the request paid before Payment Entry creation completed.
    if payment_request.stripe_payment_status == "Paid" and payment_entry_exists_for_payment_intent(payment_intent):
        return {"message": f"Payment Request {payment_request_name} already paid via invoice.paid event"}

    # Don't overwrite Cancelled status
    if payment_request.status == "Cancelled":
        frappe.db.set_value("Payment Request", payment_request_name, {
            "stripe_payment_status": "Paid",
            "stripe_payment_intent_id": payment_intent.get('id')
        }, update_modified=False)
        frappe.log_error(
            f"Stripe payment received for cancelled Payment Request {payment_request_name}. "
            "Review and refund if needed.",
            "Stripe Payment on Cancelled Request"
        )
        frappe.db.commit()
        return {"message": f"Payment Request {payment_request_name} is cancelled but Stripe collected payment — logged for review"}

    # Update status (use set_value for submitted docs)
    frappe.db.set_value("Payment Request", payment_request_name, {
        "status": "Paid",
        "stripe_payment_status": "Paid",
        "stripe_payment_intent_id": payment_intent.get('id')
    }, update_modified=False)
    
    # Fetch invoice for payment entry creation
    import stripe
    settings = frappe.get_single("Stripe Settings")
    stripe.api_key = settings.get_password("api_key")
    
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        if hasattr(invoice, "to_dict_recursive"):
            invoice = invoice.to_dict_recursive()

        # Fetch Stripe fee from charge/balance transaction
        stripe_fee = 0
        charge_id = invoice.get('charge') or payment_intent.get('latest_charge')
        if charge_id:
            try:
                charge = stripe.Charge.retrieve(charge_id)
                if charge.balance_transaction:
                    balance_txn = stripe.BalanceTransaction.retrieve(charge.balance_transaction)
                    stripe_fee = balance_txn.fee / 100  # Convert cents to dollars
            except Exception as e:
                frappe.log_error(f"Failed to fetch Stripe fee: {str(e)}", "Stripe Webhook")

        payment_entry = create_payment_entry(payment_request, invoice, stripe_fee=stripe_fee)
        result = {
            "message": "Payment recorded via payment_intent.succeeded",
            "payment_request": payment_request_name,
            "payment_entry": payment_entry.name if payment_entry else None
        }

        if stripe_fee > 0:
            try:
                result["fee_journal_entry"] = record_stripe_fee(
                    payment_request, stripe_fee, invoice_id
                )
            except Exception as e:
                frappe.log_error(f"Failed to record Stripe fee: {str(e)}", "Stripe Webhook Error")

        if payment_request.card_processing_fee:
            try:
                result["card_fee_journal_entry"] = record_card_fee_income(
                    payment_request, payment_request.card_processing_fee, invoice_id
                )
            except Exception as e:
                frappe.log_error(
                    f"Failed to record card fee income: {str(e)}",
                    "Stripe Webhook Error",
                )

        frappe.db.commit()

        return result
    except Exception as e:
        frappe.log_error(
            f"Backup payment processing failed for {payment_request_name}: {str(e)}",
            "Stripe Webhook Error"
        )
        return {"message": f"Status updated but Payment Entry creation failed: {str(e)}"}


def get_payment_intent_invoice_id(payment_intent):
    """
    Return the Stripe invoice ID represented by a PaymentIntent event.

    Some Stripe invoice payments do not populate payment_intent.invoice in the
    event payload. In that shape, Stripe sends the invoice reference under
    payment_details.order_reference.
    """
    invoice_id = payment_intent.get('invoice')
    if invoice_id:
        return invoice_id

    payment_details = payment_intent.get('payment_details') or {}
    invoice_id = payment_details.get('order_reference')
    if invoice_id:
        return invoice_id

    metadata = payment_intent.get('metadata') or {}
    return metadata.get('invoice') or metadata.get('stripe_invoice_id')


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


def payment_entry_exists_for_invoice(invoice):
    """Return whether a submitted Payment Entry exists for a Stripe invoice."""
    return payment_entry_exists_for_reference(
        invoice.get('payment_intent') or invoice.get('id')
    )


def payment_entry_exists_for_payment_intent(payment_intent):
    """Return whether a non-cancelled Payment Entry exists for a Stripe PaymentIntent."""
    return payment_entry_exists_for_reference(payment_intent.get('id'))


def payment_entry_exists_for_reference(reference_no):
    """Return whether a submitted Payment Entry exists for a Stripe reference."""
    if not reference_no:
        return False

    return bool(
        frappe.db.exists(
            "Payment Entry",
            {
                "reference_no": reference_no,
                "docstatus": 1,
            },
        )
    )


def create_payment_entry(payment_request, invoice, stripe_fee=0):
    """
    Create Payment Entry for a paid invoice.

    Args:
        payment_request: Payment Request document
        invoice: Stripe invoice object
        stripe_fee: Stripe processing fee in dollars (default 0)

    Returns:
        Payment Entry document or None
    """
    # Check if a submitted Payment Entry already exists (idempotency).
    # Draft entries do not reduce Sales Invoice outstanding balances, so they
    # must not block webhook/reconciliation recovery.
    existing = frappe.db.exists(
        "Payment Entry",
        {
            "reference_no": invoice.get('payment_intent') or invoice.get('id'),
            "docstatus": 1,
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

    # Exclude card fee — it's recorded separately as income via Journal Entry.
    # Some submitted Payment Requests have the fee fields populated while the
    # allow_card_payment checkbox is false, so the fee amount is the durable
    # source of truth.
    amount_paid = get_customer_payment_amount(payment_request, amount_paid)

    amount_paid = get_reference_allocation_amount(payment_request, amount_paid)
    
    # Get company from Payment Request
    company = payment_request.company or frappe.defaults.get_user_default("Company")
    
    if not company:
        frappe.throw(_("Company not found for Payment Entry"))
    
    # Get payment accounts - prefer clearing account from Stripe Settings
    settings = frappe.get_single("Stripe Settings")
    payment_account = settings.clearing_account if settings.clearing_account else None

    mode_of_payment = "Stripe"

    # Check if Stripe mode of payment exists, if not use Bank
    if not frappe.db.exists("Mode of Payment", "Stripe"):
        mode_of_payment = "Bank Draft"  # Fallback

    # Fallback to Mode of Payment Account if clearing account not set
    if not payment_account:
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
            f"No payment account found for company {company}. Configure 'Clearing Account' in Stripe Settings.",
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
            "remarks": f"Payment received via Stripe Invoice {invoice.get('id')}",
            "custom_stripe_fee": stripe_fee
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


def get_customer_payment_amount(payment_request, amount_paid):
    amount_paid = flt(amount_paid, 2)

    if payment_request.card_processing_fee:
        amount_paid -= flt(payment_request.card_processing_fee, 2)

    return flt(amount_paid, 2)


def get_reference_allocation_amount(payment_request, amount_paid):
    """
    Snap Stripe allocation to the invoice balance for one-cent residuals only.

    Stripe amounts and ERPNext invoice balances can differ by a cent because
    each side rounds at slightly different points. Larger differences are left
    untouched so deposits, fees, and real underpayments remain visible.
    """
    amount_paid = flt(amount_paid, 2)

    if (
        payment_request.reference_doctype != "Sales Invoice"
        or not payment_request.reference_name
    ):
        return amount_paid

    outstanding_amount = frappe.db.get_value(
        "Sales Invoice",
        payment_request.reference_name,
        "outstanding_amount",
    )

    if outstanding_amount is None:
        return amount_paid

    outstanding_amount = flt(outstanding_amount, 2)
    if outstanding_amount > 0 and abs(outstanding_amount - amount_paid) <= ROUNDING_TOLERANCE:
        return outstanding_amount

    return amount_paid


def record_stripe_fee(payment_request, fee_amount, stripe_invoice_id):
    """
    Record Stripe processing fee as an expense via Journal Entry.

    Debit: Stripe Fee Expense Account
    Credit: Stripe Clearing Account

    Args:
        payment_request: Payment Request document
        fee_amount: Fee amount in dollars
        stripe_invoice_id: Stripe invoice ID for reference

    Returns:
        str: Journal Entry name or None
    """
    settings = frappe.get_single("Stripe Settings")

    # Check if accounts are configured
    if not settings.fee_expense_account or not settings.clearing_account:
        frappe.log_error(
            "Stripe fee accounts not configured in Stripe Settings",
            "Stripe Webhook"
        )
        return None

    # Get company from Payment Request
    company = payment_request.company or frappe.defaults.get_user_default("Company")

    if not company:
        frappe.log_error("No company found for Stripe fee entry", "Stripe Webhook Error")
        return None

    # Create Journal Entry for fee
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = frappe.utils.today()
    je.user_remark = f"Stripe processing fee for invoice {stripe_invoice_id}"

    # Debit: Stripe Fee Expense Account
    je.append("accounts", {
        "account": settings.fee_expense_account,
        "debit_in_account_currency": fee_amount,
        "credit_in_account_currency": 0,
        "user_remark": f"Stripe fee for {payment_request.reference_name or payment_request.name}"
    })

    # Credit: Stripe Clearing Account
    je.append("accounts", {
        "account": settings.clearing_account,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": fee_amount,
        "user_remark": f"Stripe fee deduction"
    })

    je.insert(ignore_permissions=True)
    je.submit()

    frappe.log_error(
        f"Created Stripe fee Journal Entry {je.name} for ${fee_amount}",
        "Stripe Webhook"
    )

    return je.name


def record_card_fee_income(payment_request, fee_amount, stripe_invoice_id):
    """
    Record credit card processing fee collected from customer as income.

    This uses a simple Journal Entry approach:
    Debit: Stripe Clearing Account (already received this from customer)
    Credit: Card Fee Income Account

    Args:
        payment_request: Payment Request document
        fee_amount: Card fee amount in dollars
        stripe_invoice_id: Stripe invoice ID for reference

    Returns:
        str: Journal Entry name or None
    """
    settings = frappe.get_single("Stripe Settings")

    # Check if accounts are configured
    if not settings.card_fee_income_account or not settings.clearing_account:
        frappe.log_error(
            "Card fee income accounts not configured in Stripe Settings",
            "Stripe Webhook"
        )
        return None

    # Get company from Payment Request
    company = payment_request.company or frappe.defaults.get_user_default("Company")

    if not company:
        frappe.log_error("No company found for card fee income entry", "Stripe Webhook Error")
        return None

    # Create Journal Entry for card fee income
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = frappe.utils.today()
    je.user_remark = f"Card processing fee income for invoice {stripe_invoice_id}"

    # Debit: Stripe Clearing Account (we received this extra amount)
    je.append("accounts", {
        "account": settings.clearing_account,
        "debit_in_account_currency": fee_amount,
        "credit_in_account_currency": 0,
        "user_remark": f"Card fee received from customer"
    })

    # Credit: Card Fee Income Account
    je.append("accounts", {
        "account": settings.card_fee_income_account,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": fee_amount,
        "user_remark": f"Card processing fee income for {payment_request.reference_name or payment_request.name}"
    })

    je.insert(ignore_permissions=True)
    je.submit()

    frappe.log_error(
        f"Created card fee income Journal Entry {je.name} for ${fee_amount}",
        "Stripe Webhook"
    )

    return je.name
