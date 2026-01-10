# Copyright (c) 2026, Your Company and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class StripeSettings(Document):
    def validate(self):
        """Validate Stripe Settings."""
        if self.api_key and not self.api_key.startswith(("sk_live_", "sk_test_")):
            frappe.throw("Invalid Stripe Secret Key format. Should start with 'sk_live_' or 'sk_test_'")
        
        if self.publishable_key and not self.publishable_key.startswith(("pk_live_", "pk_test_")):
            frappe.throw("Invalid Stripe Publishable Key format. Should start with 'pk_live_' or 'pk_test_'")
        
        # Auto-detect test mode
        if self.api_key:
            self.test_mode = self.api_key.startswith("sk_test_")
    
    @staticmethod
    def get_stripe_settings():
        """Get Stripe Settings singleton."""
        settings = frappe.get_single("Stripe Settings")
        if not settings.api_key:
            frappe.throw("Stripe API Key not configured. Please configure in Stripe Settings.")
        return settings
    
    @staticmethod
    def get_stripe_client():
        """Get initialized Stripe client."""
        import stripe
        settings = StripeSettings.get_stripe_settings()
        stripe.api_key = settings.get_password("api_key")
        return stripe
