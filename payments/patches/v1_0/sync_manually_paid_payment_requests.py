import frappe

from payments.utils import (
    get_payment_status_after_external_payment,
    should_sync_external_payment_status,
)


def execute():
    """Repair Payment Requests whose reference already has a submitted Payment Entry."""
    payment_requests = frappe.db.sql(
        """
        select
            pr.name,
            pr.status,
            pr.stripe_invoice_id,
            pr.stripe_payment_status
        from `tabPayment Request` pr
        where pr.docstatus = 1
          and ifnull(pr.reference_doctype, '') != ''
          and ifnull(pr.reference_name, '') != ''
          and exists (
              select 1
              from `tabPayment Entry Reference` per
              inner join `tabPayment Entry` pe on pe.name = per.parent
              where pe.docstatus = 1
                and per.reference_doctype = pr.reference_doctype
                and per.reference_name = pr.reference_name
                and ifnull(per.allocated_amount, 0) > 0
          )
          and (
              ifnull(pr.status, '') != 'Paid'
              or ifnull(pr.stripe_payment_status, '') in ('Pending', 'N/A', '')
          )
        """,
        as_dict=True,
    )

    for payment_request in payment_requests:
        if (
            payment_request.status == "Paid"
            and not should_sync_external_payment_status(payment_request.stripe_payment_status)
        ):
            continue

        stripe_payment_status = get_payment_status_after_external_payment(
            payment_request.stripe_invoice_id
        )
        frappe.db.set_value(
            "Payment Request",
            payment_request.name,
            {
                "status": "Paid",
                "stripe_payment_status": stripe_payment_status,
            },
            update_modified=False,
        )
