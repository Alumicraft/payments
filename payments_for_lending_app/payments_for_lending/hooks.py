app_name = "payments_for_lending"
app_title = "Payments for Lending"
app_publisher = "Your Company"
app_description = "ACH Autopay integration for ERPNext Lending module"
app_email = "hello@example.com"
app_license = "MIT"
required_apps = ["frappe", "erpnext", "lending"]

# Fixtures - export custom fields
fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [
            ["name", "in", [
                "Loan-ach_payment_section",
                "Loan-ach_payment_account"
            ]]
        ]
    }
]

# Include JS in doctype views
doctype_js = {
    "Loan": "public/js/loan.js"
}

# Scheduled Tasks
scheduler_events = {
    "daily": [
        "payments_for_lending.tasks.scheduled_debits.process_upcoming_payments",
        "payments_for_lending.tasks.scheduled_debits.initiate_scheduled_transactions",
        "payments_for_lending.tasks.scheduled_debits.process_retry_transactions"
    ],
    "hourly": [
        "payments_for_lending.tasks.scheduled_debits.check_pending_transactions"
    ],
}
