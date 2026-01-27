# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ACHAuthorization(Document):
    def validate(self):
        self._validate_single_default()

    def _validate_single_default(self):
        """Ensure only one default account per customer."""
        if self.is_default and self.status == "Active":
            existing = frappe.db.exists(
                "ACH Authorization",
                {
                    "customer": self.customer,
                    "is_default": 1,
                    "status": "Active",
                    "name": ("!=", self.name)
                }
            )
            if existing:
                # Auto-unset the other default
                frappe.db.set_value("ACH Authorization", existing, "is_default", 0)

    def set_as_default(self):
        """Set this authorization as the default for the customer."""
        if self.status != "Active":
            frappe.throw(_("Only active authorizations can be set as default"))

        # Unset any existing default for this customer
        frappe.db.sql("""
            UPDATE `tabACH Authorization`
            SET is_default = 0
            WHERE customer = %s AND is_default = 1 AND name != %s
        """, (self.customer, self.name))

        self.is_default = 1
        self.save()
        return True

    def pause(self, reason=None):
        """Temporarily pause auto-pay."""
        if self.status != "Active":
            frappe.throw(_("Only active authorizations can be paused"))

        self.status = "Paused"
        if reason:
            self.add_comment("Comment", f"Authorization paused: {reason}")
        self.save()
        return True

    def resume(self):
        """Resume a paused authorization."""
        if self.status != "Paused":
            frappe.throw(_("Only paused authorizations can be resumed"))

        self.status = "Active"
        self.add_comment("Comment", "Authorization resumed")
        self.save()
        return True

    def revoke(self, reason=None):
        """Permanently revoke authorization and cancel pending transactions."""
        if self.status == "Revoked":
            frappe.throw(_("Authorization is already revoked"))

        # Check if this is the only default and customer has loans with pending payments
        if self.is_default:
            other_active = frappe.db.exists(
                "ACH Authorization",
                {
                    "customer": self.customer,
                    "status": "Active",
                    "name": ("!=", self.name)
                }
            )
            if not other_active:
                # Check for loans using default (no override)
                loans_using_default = frappe.db.count(
                    "Loan",
                    {
                        "applicant": self.customer,
                        "status": ["in", ["Disbursed", "Partially Disbursed"]],
                        "ach_payment_account": ["in", ["", None]]
                    }
                )
                if loans_using_default > 0:
                    frappe.msgprint(
                        _("Warning: {0} active loans will have no payment account after revocation. "
                          "Consider adding a new account first.").format(loans_using_default),
                        indicator="orange"
                    )

        self.status = "Revoked"
        self.is_default = 0  # Clear default flag on revocation
        self.revocation_date = now_datetime()
        self.revocation_reason = reason
        self.save()

        # Cancel any pending/scheduled transactions
        self._cancel_pending_transactions()

        self.add_comment("Comment", f"Authorization revoked: {reason or 'No reason provided'}")
        return True

    def _cancel_pending_transactions(self):
        """Cancel all pending transactions for this authorization."""
        pending_transactions = frappe.get_all(
            "ACH Transaction",
            filters={
                "ach_authorization": self.name,
                "status": ["in", ["Scheduled", "Initiated"]]
            },
            pluck="name"
        )

        for txn_name in pending_transactions:
            txn = frappe.get_doc("ACH Transaction", txn_name)
            txn.cancel_transaction("Authorization revoked")


def get_customer_default_authorization(customer):
    """Get the default ACH Authorization for a customer."""
    auth_name = frappe.db.get_value(
        "ACH Authorization",
        {"customer": customer, "is_default": 1, "status": "Active"},
        "name"
    )
    if auth_name:
        return frappe.get_doc("ACH Authorization", auth_name)
    return None


def get_loan_payment_account(loan):
    """
    Get the effective ACH Authorization for a loan.

    Resolution logic:
    1. Check loan.ach_payment_account (if active) → use it
    2. If override exists but revoked → FAIL (don't fall back)
    3. Check customer's default (if active) → use it
    4. No valid account → return None
    """
    loan_doc = frappe.get_doc("Loan", loan) if isinstance(loan, str) else loan

    # Check for loan-specific override
    if loan_doc.get("ach_payment_account"):
        auth = frappe.get_doc("ACH Authorization", loan_doc.ach_payment_account)
        if auth.status == "Active":
            return auth
        else:
            # Override exists but is not active - don't fall back silently
            frappe.log_error(
                f"Loan {loan_doc.name} has ACH override {auth.name} but it is {auth.status}",
                "ACH Payment Account Error"
            )
            return None

    # Fall back to customer's default
    return get_customer_default_authorization(loan_doc.applicant)


# Keep old function name for backward compatibility
def get_active_authorization(loan):
    """Get the active ACH Authorization for a loan (legacy function)."""
    return get_loan_payment_account(loan)
