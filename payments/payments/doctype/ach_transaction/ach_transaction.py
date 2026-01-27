# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, add_days, getdate, today

# Return codes that should not be retried
NON_RETRYABLE_RETURN_CODES = {
    "R02": "Account Closed",
    "R03": "No Account",
    "R04": "Invalid Account Number",
    "R07": "Authorization Revoked",
    "R08": "Payment Stopped",
    "R10": "Customer Advises Unauthorized",
    "R16": "Account Frozen",
    "R20": "Non-Transaction Account",
    "R29": "Corporate Not Authorized",
}


class ACHTransaction(Document):
    def validate(self):
        self._set_customer_from_authorization()
        self._set_max_retries_from_settings()

    def _set_customer_from_authorization(self):
        """Fetch customer from the authorization if not set."""
        if self.ach_authorization and not self.customer:
            self.customer = frappe.db.get_value(
                "ACH Authorization", self.ach_authorization, "customer"
            )

    def _set_max_retries_from_settings(self):
        """Set max retries from ACH Settings if this is a new transaction."""
        if self.is_new() and not self.max_retries:
            from payments.payments.doctype.ach_settings.ach_settings import get_ach_settings
            settings = get_ach_settings()
            self.max_retries = settings.max_retry_attempts

    def initiate(self):
        """Initiate the ACH transaction via ACHQ API."""
        if self.status != "Scheduled":
            frappe.throw(_("Only scheduled transactions can be initiated"))

        # Get the authorization and its token
        auth = frappe.get_doc("ACH Authorization", self.ach_authorization)
        if auth.status != "Active":
            frappe.throw(_("Authorization is not active"))

        # Import and use ACHQ client
        from payments.api.achq_integration import ACHQClient

        client = ACHQClient()
        result = client.create_payment(
            amount=self.amount,
            token=auth.get_password("achq_token"),
            customer_name=auth.customer,
            description=f"Loan payment for {self.loan}",
            txn_id=self.name
        )

        if result.get("success"):
            self.status = "Initiated"
            self.achq_transaction_id = result.get("transaction_id")
            self.achq_status = result.get("status")
            self.initiated_date = now_datetime()
            # Estimate settlement date (3-5 business days)
            self.settlement_date = add_days(today(), 5)
            self.save()
            frappe.db.commit()
            return True
        else:
            self.status = "Failed"
            self.failure_code = result.get("error_code")
            self.failure_reason = result.get("error_message")
            self.save()
            frappe.db.commit()
            return False

    def mark_success(self, achq_status=None):
        """Mark transaction as successful and create Payment Entry."""
        self.status = "Success"
        self.completed_date = now_datetime()
        if achq_status:
            self.achq_status = achq_status

        # Create Payment Entry
        payment_entry = self.create_payment_entry()
        if payment_entry:
            self.payment_entry = payment_entry.name

        self.save()

        # Send success notification
        if not self.notification_sent:
            self.send_notification("success")

        return True

    def mark_failed(self, failure_code=None, failure_reason=None, return_code=None):
        """Mark transaction as failed and schedule retry if applicable."""
        if return_code:
            self.status = "Returned"
            self.return_code = return_code
        else:
            self.status = "Failed"

        self.failure_code = failure_code
        self.failure_reason = failure_reason
        self.completed_date = now_datetime()
        self.save()

        # Check if we should schedule a retry
        if self.should_retry(return_code):
            self.schedule_retry()

        # Send failure notification
        if not self.notification_sent:
            self.send_notification("failure")

        return True

    def cancel_transaction(self, reason=None):
        """Cancel the transaction if still scheduled or initiated."""
        if self.status not in ("Scheduled", "Initiated"):
            frappe.throw(_("Only scheduled or initiated transactions can be cancelled"))

        # If initiated, try to cancel with ACHQ
        if self.status == "Initiated" and self.achq_transaction_id:
            from payments.api.achq_integration import ACHQClient
            client = ACHQClient()
            result = client.cancel_payment(self.achq_transaction_id)
            if not result.get("success"):
                frappe.log_error(
                    f"Failed to cancel ACHQ transaction {self.achq_transaction_id}: "
                    f"{result.get('error_message')}",
                    "ACH Transaction Cancellation"
                )

        self.status = "Cancelled"
        self.failure_reason = reason or "Cancelled by user"
        self.save()

        self.add_comment("Comment", f"Transaction cancelled: {reason or 'No reason provided'}")
        return True

    def should_retry(self, return_code):
        """Determine if the transaction should be retried based on return code."""
        # Don't retry if max retries reached
        if self.retry_attempt >= self.max_retries:
            return False

        # Don't retry non-retryable return codes
        if return_code and return_code in NON_RETRYABLE_RETURN_CODES:
            return False

        return True

    def schedule_retry(self):
        """Schedule a retry transaction."""
        from payments.payments.doctype.ach_settings.ach_settings import get_ach_settings
        settings = get_ach_settings()

        self.next_retry_date = add_days(today(), settings.retry_delay_days)
        self.save()

        self.add_comment(
            "Comment",
            f"Retry scheduled for {self.next_retry_date} (attempt {self.retry_attempt + 1} of {self.max_retries})"
        )

    def create_retry_transaction(self):
        """Create a new retry transaction."""
        if self.retry_attempt >= self.max_retries:
            frappe.throw(_("Maximum retry attempts reached"))

        # Check authorization is still active
        auth = frappe.get_doc("ACH Authorization", self.ach_authorization)
        if auth.status != "Active":
            frappe.throw(_("Authorization is no longer active"))

        retry_txn = frappe.new_doc("ACH Transaction")
        retry_txn.ach_authorization = self.ach_authorization
        retry_txn.loan = self.loan
        retry_txn.customer = self.customer
        retry_txn.amount = self.amount
        retry_txn.status = "Scheduled"
        retry_txn.scheduled_date = today()
        retry_txn.retry_attempt = self.retry_attempt + 1
        retry_txn.max_retries = self.max_retries
        retry_txn.original_transaction = self.original_transaction or self.name
        retry_txn.insert()

        # Clear next_retry_date on original
        self.next_retry_date = None
        self.save()

        return retry_txn

    def create_payment_entry(self):
        """Create ERPNext Payment Entry for successful transaction."""
        try:
            # Get loan details
            loan = frappe.get_doc("Loan", self.loan)

            # Check if payment entry already exists (idempotency)
            existing = frappe.db.exists(
                "Payment Entry",
                {"reference_no": self.name, "docstatus": ["!=", 2]}
            )
            if existing:
                return frappe.get_doc("Payment Entry", existing)

            payment_entry = frappe.new_doc("Payment Entry")
            payment_entry.payment_type = "Receive"
            payment_entry.party_type = "Customer"
            payment_entry.party = self.customer
            payment_entry.paid_amount = self.amount
            payment_entry.received_amount = self.amount
            payment_entry.reference_no = self.name
            payment_entry.reference_date = getdate(self.completed_date)
            payment_entry.company = loan.company

            # Set accounts - these should be configured in the system
            payment_entry.paid_to = frappe.get_cached_value(
                "Company", loan.company, "default_cash_account"
            ) or frappe.get_cached_value(
                "Company", loan.company, "default_bank_account"
            )

            # Add reference to the loan
            payment_entry.append("references", {
                "reference_doctype": "Loan",
                "reference_name": self.loan,
                "allocated_amount": self.amount
            })

            payment_entry.insert()
            payment_entry.submit()

            return payment_entry

        except Exception as e:
            frappe.log_error(
                f"Failed to create Payment Entry for ACH Transaction {self.name}: {str(e)}",
                "ACH Payment Entry Creation"
            )
            return None

    def send_notification(self, notification_type):
        """Send notification based on transaction status."""
        from payments.payments.doctype.ach_settings.ach_settings import get_ach_settings
        settings = get_ach_settings()

        should_send = False
        if notification_type == "success" and settings.send_success_notification:
            should_send = True
        elif notification_type == "failure" and settings.send_failure_notification:
            should_send = True
        elif notification_type == "upcoming" and settings.send_upcoming_debit_notification:
            should_send = True

        if not should_send:
            return

        try:
            customer_email = frappe.db.get_value("Customer", self.customer, "email_id")
            if not customer_email:
                return

            subject_map = {
                "success": f"Payment Successful - {self.amount}",
                "failure": f"Payment Failed - {self.amount}",
                "upcoming": f"Upcoming Payment - {self.amount}",
            }

            template_map = {
                "success": "ach_payment_success",
                "failure": "ach_payment_failure",
                "upcoming": "ach_payment_upcoming",
            }

            frappe.sendmail(
                recipients=[customer_email],
                subject=subject_map.get(notification_type),
                template=template_map.get(notification_type),
                args={
                    "customer": self.customer,
                    "amount": self.amount,
                    "loan": self.loan,
                    "transaction": self.name,
                    "failure_reason": self.failure_reason,
                    "scheduled_date": self.scheduled_date,
                },
                now=True
            )

            self.notification_sent = 1
            self.save()

        except Exception as e:
            frappe.log_error(
                f"Failed to send notification for ACH Transaction {self.name}: {str(e)}",
                "ACH Notification"
            )
