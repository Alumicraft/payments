app_name = "stripe_payment_integration"
app_title = "Stripe Payment Integration"
app_publisher = "Your Company"
app_description = "ERPNext Stripe Invoicing Integration with persistent payment links"
app_email = "hello@example.com"
app_license = "MIT"
required_apps = ["frappe", "erpnext"]

# Fixtures - export custom fields
fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [
            ["name", "in", [
                "Payment Request-stripe_invoice_url",
                "Payment Request-stripe_invoice_id",
                "Payment Request-stripe_payment_status",
                "Payment Request-stripe_payment_intent_id",
                "Payment Request-allow_card_payment",
                "Payment Request-card_processing_fee",
                "Payment Request-total_with_card_fee",
                "Customer-stripe_customer_id"
            ]]
        ]
    }
]

# Document Events
doc_events = {
    "Payment Request": {
        "after_insert": "stripe_payment_integration.stripe_integration.create_stripe_invoice",
        "on_update": "stripe_payment_integration.stripe_integration.handle_payment_request_update"
    }
}

# Include JS in doctype views
doctype_js = {
    "Payment Request": "public/js/payment_request.js"
}

# Include CSS
# app_include_css = "/assets/stripe_payment_integration/css/stripe.css"

# Include JS
# app_include_js = "/assets/stripe_payment_integration/js/stripe.js"

# Scheduled Tasks
scheduler_events = {
    # Uncomment if you need periodic tasks
    # "daily": [
    #     "stripe_payment_integration.tasks.daily_sync"
    # ],
}

# Jinja filters
# jinja = {
#     "methods": [
#         "stripe_payment_integration.utils.jinja_methods"
#     ]
# }

# Installation
after_install = "stripe_payment_integration.install.after_install"
before_uninstall = "stripe_payment_integration.install.before_uninstall"

# Override default routes
# override_routes = {}

# Website
# website_generators = []

# Home pages
# home_page = ""

# Desk Notifications
# notification_config = "stripe_payment_integration.notifications.get_notification_config"
