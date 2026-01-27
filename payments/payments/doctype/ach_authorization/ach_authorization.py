# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ACHAuthorization(Document):
    def validate(self):
        self._validate_loan_customer()
        self._validate_unique_active_authorization()

    def _validate_loan_customer(self):
        """Ensure the loan belongs to the specified customer."""
        if self.loan:
            loan_customer = frappe.db.get_value("Loan", self.loan, "applicant")
            if loan_customer != self.customer:
                frappe.throw(_("Loan {0} does not belong to Customer {1}").format(
                    self.loan, self.customer
                ))

    def _validate_unique_active_authorization(self):
        """Ensure only one active authorization per loan."""
        if self.status == "Active":
            existing = frappe.db.exists(
                "ACH Authorization",
                {
                    "loan": self.loan,
                    "status": "Active",
                    "name": ("!=", self.name)
                }
            )
            if existing:
                frappe.throw(
                    _("An active ACH Authorization already exists for Loan {0}").format(
                        self.loan
                    )
                )

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

        # Check no other active authorization exists
        existing = frappe.db.exists(
            "ACH Authorization",
            {
                "loan": self.loan,
                "status": "Active",
                "name": ("!=", self.name)
            }
        )
        if existing:
            frappe.throw(
                _("Another active authorization exists for this loan. "
                  "Please revoke it first.")
            )

        self.status = "Active"
        self.add_comment("Comment", "Authorization resumed")
        self.save()
        return True

    def revoke(self, reason=None):
        """Permanently revoke authorization and cancel pending transactions."""
        if self.status == "Revoked":
            frappe.throw(_("Authorization is already revoked"))

        self.status = "Revoked"
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


def get_active_authorization(loan):
    """Get the active ACH Authorization for a loan."""
    auth_name = frappe.db.get_value(
        "ACH Authorization",
        {"loan": loan, "status": "Active"},
        "name"
    )
    if auth_name:
        return frappe.get_doc("ACH Authorization", auth_name)
    return None
