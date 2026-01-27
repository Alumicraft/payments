# Copyright (c) 2024, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ACHSettings(Document):
    def validate(self):
        if self.enable_ach_autopay:
            self._validate_required_fields()
            self._validate_scheduling_settings()

    def _validate_required_fields(self):
        """Ensure required ACHQ credentials are provided when enabled."""
        required_fields = [
            ("achq_provider_id", "Provider ID"),
            ("achq_provider_gate_id", "Provider Gate ID"),
            ("achq_provider_gate_key", "Provider Gate Key"),
            ("achq_merchant_id", "Merchant ID"),
            ("achq_merchant_gate_id", "Merchant Gate ID"),
            ("achq_merchant_gate_key", "Merchant Gate Key"),
        ]
        for field, label in required_fields:
            if not self.get(field):
                frappe.throw(f"{label} is required when ACH Autopay is enabled")

    def _validate_scheduling_settings(self):
        """Validate scheduling settings are reasonable."""
        if self.days_before_due_to_initiate < 1:
            frappe.throw("Days before due to initiate must be at least 1")
        if self.advance_notification_days < self.days_before_due_to_initiate:
            frappe.throw(
                "Advance notification days must be greater than or equal to "
                "days before due to initiate"
            )
        if self.max_retry_attempts < 0:
            frappe.throw("Max retry attempts cannot be negative")
        if self.retry_delay_days < 1:
            frappe.throw("Retry delay days must be at least 1")


def get_ach_settings():
    """Get the ACH Settings singleton."""
    return frappe.get_single("ACH Settings")


def is_ach_enabled():
    """Check if ACH autopay is enabled."""
    settings = get_ach_settings()
    return settings.enable_ach_autopay
