# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

"""
Scheduled Tasks for ACH Processing

Daily tasks:
- process_upcoming_payments: Create transactions for upcoming loan payments
- initiate_scheduled_transactions: Send scheduled transactions to ACHQ
- process_retry_transactions: Retry failed transactions

Hourly tasks:
- check_pending_transactions: Poll ACHQ for status updates (backup to webhooks)
"""

import frappe
from frappe.utils import today, add_days, getdate, now_datetime, get_time, nowtime


def is_ach_enabled():
    """Check if ACH autopay is enabled."""
    try:
        settings = frappe.get_single("ACH Settings")
        return settings.enable_ach_autopay
    except Exception:
        return False


def process_upcoming_payments():
    """
    Find loans with payments due soon and create ACH Transactions.

    This runs daily and:
    1. Finds active loans that have a valid payment account (loan-specific or customer default)
    2. Checks for upcoming payments based on advance_notification_days
    3. Creates ACH Transactions for each upcoming payment
    4. Sends advance notification to customers
    """
    if not is_ach_enabled():
        return

    from payments.payments.doctype.ach_authorization.ach_authorization import get_loan_payment_account

    settings = frappe.get_single("ACH Settings")
    notification_days = settings.advance_notification_days
    initiation_days = settings.days_before_due_to_initiate

    # Calculate the target due date range
    # We want to notify for payments due in `notification_days` days
    target_date = add_days(today(), notification_days)

    frappe.logger().info(f"Processing upcoming payments for due date: {target_date}")

    # Find all active loans (we'll resolve payment accounts per-loan)
    active_loans = frappe.get_all(
        "Loan",
        filters={
            "status": ["in", ["Disbursed", "Partially Disbursed"]]
        },
        fields=["name", "applicant"]
    )

    for loan_data in active_loans:
        try:
            # Get the effective payment account for this loan
            auth = get_loan_payment_account(loan_data.name)

            if not auth:
                # No valid payment account for this loan
                continue

            if auth.status != "Active":
                continue

            process_loan_payment(loan_data, auth, target_date, initiation_days)
        except Exception as e:
            frappe.log_error(
                f"Error processing loan {loan_data.name}: {str(e)}",
                "ACH Process Upcoming Payments"
            )

    frappe.db.commit()


def process_loan_payment(loan_data, auth, target_date, initiation_days):
    """
    Process a single loan for upcoming payment.

    Args:
        loan_data: Loan dict with name and applicant
        auth: ACH Authorization document (resolved for this loan)
        target_date: The due date we're looking for
        initiation_days: Days before due to schedule initiation
    """
    loan = frappe.get_doc("Loan", loan_data.name)

    # Check if loan has a payment due on the target date
    # This depends on your Loan doctype structure
    # Adjust the field names based on your actual implementation
    next_payment_date = None
    next_payment_amount = None

    # Try to get next repayment date from loan
    if hasattr(loan, 'next_payment_date'):
        next_payment_date = getdate(loan.next_payment_date)
    elif hasattr(loan, 'repayment_start_date'):
        # Calculate based on repayment schedule if available
        next_payment_date = get_next_repayment_date(loan)

    if not next_payment_date:
        return

    # Check if the payment is due on our target date
    if next_payment_date != getdate(target_date):
        return

    # Get payment amount
    if hasattr(loan, 'monthly_repayment_amount'):
        next_payment_amount = loan.monthly_repayment_amount
    elif hasattr(loan, 'total_payment'):
        next_payment_amount = loan.total_payment
    else:
        frappe.log_error(
            f"Cannot determine payment amount for loan {loan.name}",
            "ACH Process Upcoming Payments"
        )
        return

    if not next_payment_amount or next_payment_amount <= 0:
        return

    # Check if transaction already exists for this payment
    existing = frappe.db.exists(
        "ACH Transaction",
        {
            "loan": loan.name,
            "scheduled_date": [">=", add_days(target_date, -initiation_days)],
            "status": ["not in", ["Cancelled", "Failed", "Returned"]]
        }
    )
    if existing:
        return

    # Create ACH Transaction
    scheduled_date = add_days(target_date, -initiation_days)

    txn = frappe.new_doc("ACH Transaction")
    txn.ach_authorization = auth.name
    txn.loan = loan.name
    txn.customer = loan.applicant
    txn.amount = next_payment_amount
    txn.status = "Scheduled"
    txn.scheduled_date = scheduled_date
    txn.insert()

    frappe.logger().info(
        f"Created ACH Transaction {txn.name} for loan {loan.name}, "
        f"amount {next_payment_amount}, scheduled for {scheduled_date}"
    )

    # Send upcoming debit notification
    txn.send_notification("upcoming")


def get_next_repayment_date(loan):
    """
    Calculate the next repayment date for a loan.

    This is a placeholder - adjust based on your actual Loan doctype structure.
    """
    # If there's a repayment schedule child table
    if hasattr(loan, 'repayment_schedule') and loan.repayment_schedule:
        for row in loan.repayment_schedule:
            if getdate(row.payment_date) >= getdate(today()) and not row.is_paid:
                return getdate(row.payment_date)

    return None


def initiate_scheduled_transactions():
    """
    Initiate ACH Transactions that are scheduled for today.

    This runs daily and:
    1. Finds transactions with status=Scheduled and scheduled_date<=today
    2. Checks if we're before the cutoff time
    3. Initiates each transaction via ACHQ API
    """
    if not is_ach_enabled():
        return

    settings = frappe.get_single("ACH Settings")

    # Check cutoff time
    cutoff_time = settings.cutoff_time
    if cutoff_time:
        current_time = get_time(nowtime())
        if current_time > cutoff_time:
            frappe.logger().info(
                f"Past cutoff time ({cutoff_time}), skipping initiation until tomorrow"
            )
            return

    # Find scheduled transactions ready to initiate
    transactions = frappe.get_all(
        "ACH Transaction",
        filters={
            "status": "Scheduled",
            "scheduled_date": ["<=", today()]
        },
        pluck="name"
    )

    frappe.logger().info(f"Found {len(transactions)} transactions to initiate")

    for txn_name in transactions:
        try:
            txn = frappe.get_doc("ACH Transaction", txn_name)

            # Verify authorization is still active
            auth = frappe.get_doc("ACH Authorization", txn.ach_authorization)
            if auth.status != "Active":
                frappe.logger().warning(
                    f"Skipping transaction {txn_name}: authorization {auth.name} is {auth.status}"
                )
                continue

            # Initiate the transaction
            success = txn.initiate()
            if success:
                frappe.logger().info(f"Initiated transaction {txn_name}")
            else:
                frappe.logger().warning(f"Failed to initiate transaction {txn_name}")

        except Exception as e:
            frappe.log_error(
                f"Error initiating transaction {txn_name}: {str(e)}",
                "ACH Initiate Transactions"
            )

    frappe.db.commit()


def process_retry_transactions():
    """
    Process transactions that are due for retry.

    This runs daily and:
    1. Finds failed/returned transactions with next_retry_date<=today
    2. Verifies retry attempts haven't exceeded max
    3. Creates new retry transactions
    """
    if not is_ach_enabled():
        return

    # Find transactions ready for retry
    transactions = frappe.get_all(
        "ACH Transaction",
        filters={
            "status": ["in", ["Failed", "Returned"]],
            "next_retry_date": ["<=", today()],
            "next_retry_date": ["!=", None]
        },
        fields=["name", "retry_attempt", "max_retries", "ach_authorization"]
    )

    frappe.logger().info(f"Found {len(transactions)} transactions for retry")

    for txn_data in transactions:
        try:
            # Check if we can retry
            if txn_data.retry_attempt >= txn_data.max_retries:
                continue

            # Check authorization is still active
            auth_status = frappe.db.get_value(
                "ACH Authorization",
                txn_data.ach_authorization,
                "status"
            )
            if auth_status != "Active":
                frappe.logger().warning(
                    f"Skipping retry for {txn_data.name}: authorization is {auth_status}"
                )
                # Clear the next_retry_date
                frappe.db.set_value(
                    "ACH Transaction",
                    txn_data.name,
                    "next_retry_date",
                    None
                )
                continue

            # Create retry transaction
            txn = frappe.get_doc("ACH Transaction", txn_data.name)
            retry_txn = txn.create_retry_transaction()

            frappe.logger().info(
                f"Created retry transaction {retry_txn.name} for {txn_data.name}"
            )

        except Exception as e:
            frappe.log_error(
                f"Error creating retry for {txn_data.name}: {str(e)}",
                "ACH Retry Transactions"
            )

    frappe.db.commit()


def check_pending_transactions():
    """
    Poll ACHQ for status updates on pending transactions.

    This is a backup to webhooks and runs hourly:
    1. Queries ACHQ for all status changes today and yesterday
    2. Matches transactions by reference ID
    3. Updates transactions if status has changed
    """
    if not is_ach_enabled():
        return

    from payments.api.achq_integration import ACHQClient, ACHQ_STATUS_MAP

    try:
        client = ACHQClient()
    except Exception as e:
        frappe.log_error(
            f"Failed to initialize ACHQ client: {str(e)}",
            "ACH Check Pending"
        )
        return

    # Query status updates for today and yesterday
    dates_to_check = [today(), add_days(today(), -1)]

    for check_date in dates_to_check:
        try:
            result = client.get_status_by_date(check_date)

            if not result.get("success"):
                frappe.logger().warning(
                    f"Failed to get status for {check_date}: {result.get('error_message')}"
                )
                continue

            transactions = result.get("transactions", [])
            frappe.logger().info(f"Got {len(transactions)} status updates for {check_date}")

            for achq_txn in transactions:
                process_achq_status_update(achq_txn)

        except Exception as e:
            frappe.log_error(
                f"Error checking status for {check_date}: {str(e)}",
                "ACH Check Pending"
            )

    frappe.db.commit()


def process_achq_status_update(achq_txn):
    """
    Process a single ACHQ transaction status update.

    Args:
        achq_txn: Transaction dict from ACHQ status query
    """
    from payments.api.achq_integration import ACHQ_STATUS_MAP

    # Try to find our transaction by ACHQ transaction ID or merchant reference
    achq_transaction_id = achq_txn.get("TransactionID")
    merchant_ref_id = achq_txn.get("Merchant_ReferenceID")

    txn_name = None

    # First try merchant reference (our internal name)
    if merchant_ref_id and frappe.db.exists("ACH Transaction", merchant_ref_id):
        txn_name = merchant_ref_id
    elif achq_transaction_id:
        txn_name = frappe.db.get_value(
            "ACH Transaction",
            {"achq_transaction_id": achq_transaction_id},
            "name"
        )

    if not txn_name:
        return  # Transaction not found in our system

    txn = frappe.get_doc("ACH Transaction", txn_name)

    # Skip if already in a final state
    if txn.status in ("Success", "Cancelled"):
        return

    achq_status = achq_txn.get("PaymentStatus", "")
    mapped_status = ACHQ_STATUS_MAP.get(achq_status)

    if not mapped_status:
        # Unknown status, just update the raw status
        if txn.achq_status != achq_status:
            txn.achq_status = achq_status
            txn.save()
        return

    try:
        if mapped_status == "Success" and txn.status != "Success":
            txn.mark_success(achq_status=achq_status)
            frappe.logger().info(f"Transaction {txn_name} marked as success")

        elif mapped_status == "Returned" and txn.status not in ("Returned", "Failed"):
            txn.mark_failed(
                return_code=achq_txn.get("ReturnCode"),
                failure_reason=achq_txn.get("ReturnDescription"),
            )
            frappe.logger().info(f"Transaction {txn_name} marked as returned")

        elif mapped_status == "Processing" and txn.status == "Initiated":
            txn.status = "Processing"
            txn.achq_status = achq_status
            txn.save()

        elif mapped_status == "Cancelled" and txn.status not in ("Cancelled", "Success"):
            txn.status = "Cancelled"
            txn.achq_status = achq_status
            txn.save()

        elif mapped_status == "Failed" and txn.status not in ("Failed", "Returned", "Success"):
            txn.mark_failed(
                failure_code=achq_txn.get("ResponseCode"),
                failure_reason=achq_txn.get("Description", "Payment failed"),
            )
            frappe.logger().info(f"Transaction {txn_name} marked as failed")

    except Exception as e:
        frappe.log_error(
            f"Error updating transaction {txn_name}: {str(e)}",
            "ACH Status Update"
        )
