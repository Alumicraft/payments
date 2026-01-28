import frappe


def after_install():
    """Create custom fields after app installation"""
    create_custom_fields()


def before_uninstall():
    """Remove custom fields before app uninstallation"""
    delete_custom_fields()


def create_custom_fields():
    """Create custom fields for Loan doctype"""
    custom_fields = {
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

    for doctype, fields in custom_fields.items():
        for field in fields:
            field_name = f"{doctype}-{field['fieldname']}"
            if not frappe.db.exists("Custom Field", field_name):
                custom_field = frappe.new_doc("Custom Field")
                custom_field.dt = doctype
                for key, value in field.items():
                    setattr(custom_field, key, value)
                custom_field.insert(ignore_permissions=True)
                frappe.db.commit()


def delete_custom_fields():
    """Remove custom fields created by this app"""
    fields_to_delete = [
        "Loan-ach_payment_section",
        "Loan-ach_payment_account"
    ]

    for field_name in fields_to_delete:
        if frappe.db.exists("Custom Field", field_name):
            frappe.delete_doc("Custom Field", field_name, force=True)
            frappe.db.commit()
