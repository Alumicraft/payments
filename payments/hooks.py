app_name = "payments"
app_title = "Payments"
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
        "on_submit": "payments.utils.create_stripe_invoice",
        "on_update": "payments.utils.handle_payment_request_update"
    }
}

# Include JS in doctype views
doctype_js = {
    "Payment Request": "public/js/payment_request.js",
    "Loan": "public/js/loan.js"
}

# Include CSS
# app_include_css = "/assets/payments/css/stripe.css"

# Include JS
# app_include_js = "/assets/payments/js/stripe.js"

# Scheduled Tasks
scheduler_events = {
    "daily": [
        "payments.tasks.scheduled_debits.process_upcoming_payments",
        "payments.tasks.scheduled_debits.initiate_scheduled_transactions",
        "payments.tasks.scheduled_debits.process_retry_transactions"
    ],
    "hourly": [
        "payments.tasks.scheduled_debits.check_pending_transactions"
    ],
}

# Jinja filters
# jinja = {
#     "methods": [
#         "payments.utils.jinja_methods"
#     ]
# }

# Installation
after_install = "payments.install.after_install"
before_uninstall = "payments.install.before_uninstall"

# Override default routes
# override_routes = {}

# Website
# website_generators = []

# Home pages
# home_page = ""

# Desk Notifications
# notification_config = "payments.notifications.get_notification_config"
