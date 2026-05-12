import frappe


def execute():
    """Repair paid Payment Requests whose custom Stripe status is still pending."""
    frappe.db.sql(
        """
        update `tabPayment Request`
        set stripe_payment_status = 'Paid'
        where docstatus = 1
          and status = 'Paid'
          and ifnull(outstanding_amount, 0) = 0
          and stripe_payment_status = 'Pending'
          and ifnull(stripe_invoice_id, '') != ''
        """
    )
