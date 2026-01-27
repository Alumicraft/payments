import frappe


def after_install():
    """Run after app installation."""
    create_custom_fields()
    frappe.db.commit()
    print("Stripe Payment Integration installed successfully!")


def before_uninstall():
    """Run before app uninstallation."""
    # Optionally remove custom fields
    # delete_custom_fields()
    print("Stripe Payment Integration uninstalling...")


def create_custom_fields():
    """Create custom fields for Payment Request and Customer DocTypes."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as _create_custom_fields
    
    custom_fields = {
        "Payment Request": [
            {
                "fieldname": "stripe_section",
                "label": "Payment Settings",
                "fieldtype": "Section Break",
                "insert_after": "payment_url",
                "collapsible": 1
            },
            {
                "fieldname": "stripe_invoice_url",
                "label": "Stripe Invoice URL",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "stripe_section",
                "options": "URL"
            },
            {
                "fieldname": "stripe_invoice_id",
                "label": "Stripe Invoice ID",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "stripe_invoice_url"
            },
            {
                "fieldname": "stripe_payment_status",
                "label": "Stripe Payment Status",
                "fieldtype": "Select",
                "options": "\nPending\nPaid\nFailed\nVoided\nAction Required",
                "read_only": 1,
                "insert_after": "stripe_invoice_id"
            },
            {
                "fieldname": "stripe_payment_intent_id",
                "label": "Stripe Payment Intent ID",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "stripe_payment_status"
            },
            {
                "fieldname": "stripe_column_break",
                "fieldtype": "Column Break",
                "insert_after": "stripe_payment_intent_id"
            },
            {
                "fieldname": "allow_card_payment",
                "label": "Allow Card Payment (+3% fee)",
                "fieldtype": "Check",
                "default": 0,
                "insert_after": "stripe_column_break",
                "description": "Enable card payments with 3% processing fee passed to customer"
            },
            {
                "fieldname": "card_processing_fee",
                "label": "Card Processing Fee",
                "fieldtype": "Currency",
                "read_only": 1,
                "insert_after": "allow_card_payment",
                "depends_on": "allow_card_payment"
            },
            {
                "fieldname": "total_with_card_fee",
                "label": "Total with Card Fee",
                "fieldtype": "Currency",
                "read_only": 1,
                "insert_after": "card_processing_fee",
                "depends_on": "allow_card_payment"
            }
        ],
        "Customer": [
            {
                "fieldname": "stripe_customer_id",
                "label": "Stripe Customer ID",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "customer_name"
            }
        ],
        "Loan": [
            {
                "fieldname": "ach_payment_section",
                "label": "ACH Payment",
                "fieldtype": "Section Break",
                "insert_after": "repayment_method",
                "collapsible": 1
            },
            {
                "fieldname": "ach_payment_account",
                "label": "ACH Payment Account",
                "fieldtype": "Link",
                "options": "ACH Authorization",
                "insert_after": "ach_payment_section",
                "description": "Specific bank account for this loan (leave blank to use customer's default)"
            }
        ]
    }
    
    _create_custom_fields(custom_fields)


def delete_custom_fields():
    """Remove custom fields on uninstall."""
    fields_to_delete = [
        "Payment Request-stripe_section",
        "Payment Request-stripe_invoice_url",
        "Payment Request-stripe_invoice_id",
        "Payment Request-stripe_payment_status",
        "Payment Request-stripe_payment_intent_id",
        "Payment Request-stripe_column_break",
        "Payment Request-allow_card_payment",
        "Payment Request-card_processing_fee",
        "Payment Request-total_with_card_fee",
        "Customer-stripe_customer_id",
        "Loan-ach_payment_section",
        "Loan-ach_payment_account"
    ]
    
    for field in fields_to_delete:
        if frappe.db.exists("Custom Field", field):
            frappe.delete_doc("Custom Field", field)
